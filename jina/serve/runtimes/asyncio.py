import argparse
import asyncio
import os
import signal
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional, Union

from grpc import RpcError

from jina import __windows__
from jina.helper import send_telemetry_event
from jina.importer import ImportExtensions
from jina.serve.networking import GrpcConnectionPool
from jina.serve.runtimes.base import BaseRuntime
from jina.serve.runtimes.monitoring import MonitoringMixin
from jina.types.request.data import DataRequest

if TYPE_CHECKING:
    import multiprocessing
    import threading


class AsyncNewLoopRuntime(BaseRuntime, MonitoringMixin, ABC):
    """
    The async runtime to start a new event loop.
    """

    def __init__(
        self,
        args: 'argparse.Namespace',
        cancel_event: Optional[
            Union['asyncio.Event', 'multiprocessing.Event', 'threading.Event']
        ] = None,
        **kwargs,
    ):
        super().__init__(args, **kwargs)
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self.is_cancel = cancel_event or asyncio.Event()
        if not __windows__:
            # TODO: windows event loops don't support signal handlers
            try:
                for signame in {'SIGINT', 'SIGTERM'}:
                    self._loop.add_signal_handler(
                        getattr(signal, signame),
                        lambda *args, **kwargs: self.is_cancel.set(),
                    )
            except (ValueError, RuntimeError) as exc:
                self.logger.warning(
                    f' The runtime {self.__class__.__name__} will not be able to handle termination signals. '
                    f' {repr(exc)}'
                )
        else:
            with ImportExtensions(
                required=True,
                logger=self.logger,
                help_text='''If you see a 'DLL load failed' error, please reinstall `pywin32`.
                If you're using conda, please use the command `conda install -c anaconda pywin32`''',
            ):
                import win32api

            win32api.SetConsoleCtrlHandler(
                lambda *args, **kwargs: self.is_cancel.set(), True
            )

        self._setup_monitoring()
        send_telemetry_event(event='start', obj=self, entity_id=self._entity_id)
        self._start_time = time.time()
        self._loop.run_until_complete(self.async_setup())

    def run_forever(self):
        """
        Running method to block the main thread.

        Run the event loop until a Future is done.
        """
        self._loop.run_until_complete(self._loop_body())

    def teardown(self):
        """Call async_teardown() and stop and close the event loop."""
        self._loop.run_until_complete(self.async_teardown())
        self._loop.stop()
        self._loop.close()
        super().teardown()
        self._stop_time = time.time()
        send_telemetry_event(
            event='stop',
            obj=self,
            duration=self._stop_time - self._start_time,
            entity_id=self._entity_id,
        )

    async def _wait_for_cancel(self):
        """Do NOT override this method when inheriting from :class:`GatewayPod`"""
        # threads are not using asyncio.Event, but threading.Event
        if isinstance(self.is_cancel, asyncio.Event):
            await self.is_cancel.wait()
        else:
            while not self.is_cancel.is_set():
                await asyncio.sleep(0.1)

        await self.async_cancel()

    async def _loop_body(self):
        """Do NOT override this method when inheriting from :class:`GatewayPod`"""
        try:
            await asyncio.gather(self.async_run_forever(), self._wait_for_cancel())
        except asyncio.CancelledError:
            self.logger.warning('received terminate ctrl message from main process')

    def _cancel(self):
        """
        Signal the runtime to terminate
        """
        self.is_cancel.set()

    async def async_setup(self):
        """The async method to setup."""
        pass

    async def async_teardown(self):
        """The async method to clean up resources during teardown. This method should free all resources allocated
        during async_setup"""
        pass

    @abstractmethod
    async def async_cancel(self):
        """An async method to cancel async_run_forever."""
        ...

    @abstractmethod
    async def async_run_forever(self):
        """The async method to run until it is stopped."""
        ...

    # Static methods used by the Pod to communicate with the `Runtime` in the separate process

    @staticmethod
    def activate(**kwargs):
        """
        Activate the runtime, does not apply to these runtimes

        :param kwargs: extra keyword arguments
        """
        # does not apply to this types of runtimes
        pass

    @staticmethod
    def is_ready(ctrl_address: str, **kwargs) -> bool:
        """
        Check if status is ready.

        :param ctrl_address: the address where the control request needs to be sent
        :param kwargs: extra keyword arguments

        :return: True if status is ready else False.
        """

        try:
            from grpc_health.v1 import health_pb2, health_pb2_grpc

            response = GrpcConnectionPool.send_health_check_sync(
                ctrl_address, timeout=1.0
            )
            # TODO: Get the proper value of the ServingStatus SERVING KEY
            return response.status == 1
        except RpcError:
            return False

    @classmethod
    def wait_for_ready_or_shutdown(
        cls,
        timeout: Optional[float],
        ready_or_shutdown_event: Union['multiprocessing.Event', 'threading.Event'],
        ctrl_address: str,
        **kwargs,
    ):
        """
        Check if the runtime has successfully started

        :param timeout: The time to wait before readiness or failure is determined
        :param ctrl_address: the address where the control message needs to be sent
        :param ready_or_shutdown_event: the multiprocessing event to detect if the process failed or is ready
        :param kwargs: extra keyword arguments
        :return: True if is ready or it needs to be shutdown
        """
        timeout_ns = 1000000000 * timeout if timeout else None
        now = time.time_ns()
        while timeout_ns is None or time.time_ns() - now < timeout_ns:
            if ready_or_shutdown_event.is_set() or cls.is_ready(ctrl_address, **kwargs):
                return True
            time.sleep(0.1)
        return False

    def _log_info_msg(self, request: DataRequest):
        self._log_data_request(request)

    def _log_data_request(self, request: DataRequest):
        self.logger.debug(
            f'recv DataRequest at {request.header.exec_endpoint} with id: {request.header.request_id}'
        )

    @property
    def _entity_id(self):
        import uuid

        if hasattr(self, '_entity_id_'):
            return self._entity_id_
        self._entity_id_ = uuid.uuid1().hex
        return self._entity_id_
