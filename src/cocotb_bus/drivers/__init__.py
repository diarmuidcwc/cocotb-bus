# Copyright cocotb contributors
# Copyright (c) 2013 Potential Ventures Ltd
# Copyright (c) 2013 SolarFlare Communications Inc
# Licensed under the Revised BSD License, see LICENSE for details.
# SPDX-License-Identifier: BSD-3-Clause

"""Set of common driver base classes."""

from collections import deque
from typing import Iterable, Tuple, Any, Optional, Callable

import cocotb
from cocotb.decorators import coroutine
from cocotb.triggers import (Event, RisingEdge, ReadOnly, NextTimeStep,
                             Edge)
from cocotb.log import SimLog
from cocotb.handle import SimHandleBase

from cocotb_bus.bus import Bus


class BitDriver:
    """Drives a signal onto a single bit.

    Useful for exercising ready/valid flags.
    """

    def __init__(self, signal, clk, generator=None):
        self._signal = signal
        self._clk = clk
        self._generator = generator
        self._cr = None

    def start(self, generator: Iterable[Tuple[int, int]] = None) -> None:
        """Start generating data.

        Args:
            generator: Generator yielding data.
                The generator should yield tuples ``(on, off)``
                with the number of cycles to be on,
                followed by the number of cycles to be off.
                Typically the generator should go on forever.

                Example::

                    bit_driver.start((1, i % 5) for i in itertools.count())
        """
        self._cr = cocotb.fork(self._cr_twiddler(generator=generator))

    def stop(self):
        """Stop generating data."""
        self._cr.kill()

    async def _cr_twiddler(self, generator=None):
        if generator is None and self._generator is None:
            raise Exception("No generator provided!")
        if generator is not None:
            self._generator = generator

        edge = RisingEdge(self._clk)

        # Actual thread
        while True:
            on, off = next(self._generator)
            self._signal.value = 1
            for _ in range(on):
                await edge
            self._signal.value = 0
            for _ in range(off):
                await edge


class Driver:
    """Class defining the standard interface for a driver within a testbench.

    The driver is responsible for serializing transactions onto the physical
    pins of the interface.  This may consume simulation time.
    """

    def __init__(self):
        """Constructor for a driver instance."""
        self._pending = Event(name="Driver._pending")
        self._sendQ = deque()
        self.busy_event = Event("Driver._busy")
        self.busy = False

        # Sub-classes may already set up logging
        if not hasattr(self, "log"):
            self.log = SimLog("cocotb.driver.%s" % (type(self).__qualname__))

        # Create an independent coroutine which can send stuff
        self._thread = cocotb.scheduler.start_soon(self._send_thread())

    async def _acquire_lock(self):
        if self.busy:
            await self.busy_event.wait()
        self.busy_event.clear()
        self.busy = True

    def _release_lock(self):
        self.busy = False
        self.busy_event.set()

    def kill(self):
        """Kill the coroutine sending stuff."""
        if self._thread:
            self._thread.kill()
            self._thread = None

    def append(
        self, transaction: Any, callback: Callable[[Any], Any] = None,
        event: Event = None, **kwargs: Any
    ) -> None:
        """Queue up a transaction to be sent over the bus.

        Mechanisms are provided to permit the caller to know when the
        transaction is processed.

        Args:
            transaction: The transaction to be sent.
            callback: Optional function to be called
                when the transaction has been sent.
            event: :class:`~cocotb.triggers.Event` to be set
                when the transaction has been sent.
            **kwargs: Any additional arguments used in child class'
                :any:`_driver_send` method.
        """
        self._sendQ.append((transaction, callback, event, kwargs))
        self._pending.set()

    def clear(self):
        """Clear any queued transactions without sending them onto the bus."""
        self._sendQ = deque()

    @coroutine
    async def send(self, transaction: Any, sync: bool = True, **kwargs: Any) -> None:
        """Blocking send call (hence must be "awaited" rather than called).

        Sends the transaction over the bus.

        Args:
            transaction: The transaction to be sent.
            sync: Synchronize the transfer by waiting for a rising edge.
            **kwargs: Additional arguments used in child class'
                :any:`_driver_send` method.
        """
        await self._send(transaction, None, None, sync=sync, **kwargs)

    async def _driver_send(self, transaction: Any, sync: bool = True, **kwargs: Any) -> None:
        """Actual implementation of the send.

        Sub-classes should override this method to implement the actual
        :meth:`~cocotb.drivers.Driver.send` routine.

        Args:
            transaction: The transaction to be sent.
            sync: Synchronize the transfer by waiting for a rising edge.
            **kwargs: Additional arguments if required for protocol implemented in a sub-class.
        """
        raise NotImplementedError("Sub-classes of Driver should define a "
                                  "_driver_send coroutine")

    async def _send(
        self, transaction: Any, callback: Callable[[Any], Any], event: Event,
        sync: bool = True, **kwargs
    ) -> None:
        """Send coroutine.

        Args:
            transaction: The transaction to be sent.
            callback: Optional function to be called
                when the transaction has been sent.
            event: event to be set when the transaction has been sent.
            sync: Synchronize the transfer by waiting for a rising edge.
            **kwargs: Any additional arguments used in child class'
                :any:`_driver_send` method.
        """
        await self._driver_send(transaction, sync=sync, **kwargs)

        # Notify the world that this transaction is complete
        if event:
            event.set()
        if callback:
            callback(transaction)

    async def _send_thread(self):
        while True:

            # Sleep until we have something to send
            while not self._sendQ:
                self._pending.clear()
                await self._pending.wait()

            synchronised = False

            # Send in all the queued packets,
            # only synchronize on the first send
            while self._sendQ:
                transaction, callback, event, kwargs = self._sendQ.popleft()
                self.log.debug("Sending queued packet...")
                await self._send(transaction, callback, event,
                                 sync=not synchronised, **kwargs)
                synchronised = True


class BusDriver(Driver):
    """Wrapper around common functionality for buses which have:

        * a list of :attr:`_signals` (class attribute)
        * a list of :attr:`_optional_signals` (class attribute)
        * a clock
        * a name
        * an entity

    Args:
        entity: A handle to the simulator entity.
        name: Name of this bus. ``None`` for a nameless bus, e.g.
            bus-signals in an interface or a ``modport``.
            (untested on ``struct``/``record``, but could work here as well).
        clock: A handle to the clock associated with this bus.
        **kwargs: Keyword arguments forwarded to :class:`cocotb.Bus`,
            see docs for that class for more information.

    """

    _optional_signals = []

    def __init__(self, entity: SimHandleBase, name: Optional[str], clock: SimHandleBase, **kwargs: Any):
        index = kwargs.get("array_idx", None)

        self.log = SimLog("cocotb.%s.%s" % (entity._name, name))
        Driver.__init__(self)
        self.entity = entity
        self.clock = clock
        self.bus = Bus(
            self.entity, name, self._signals, optional_signals=self._optional_signals,
            **kwargs
        )

        # Give this instance a unique name
        self.name = name if index is None else "%s_%d" % (name, index)

    async def _driver_send(self, transaction, sync: bool = True) -> None:
        """Implementation for BusDriver.

        Args:
            transaction: The transaction to send.
            sync: Synchronize the transfer by waiting for a rising edge.
        """
        if sync:
            await RisingEdge(self.clock)
        self.bus.value = transaction

    @coroutine
    async def _wait_for_signal(self, signal):
        """This method will return when the specified signal
        has hit logic ``1``. The state will be in the
        :class:`~cocotb.triggers.ReadOnly` phase so sim will need
        to move to :class:`~cocotb.triggers.NextTimeStep` before
        registering more callbacks can occur.
        """
        await ReadOnly()
        while signal.value.integer != 1:
            await RisingEdge(signal)
            await ReadOnly()
        await NextTimeStep()

    @coroutine
    async def _wait_for_nsignal(self, signal):
        """This method will return when the specified signal
        has hit logic ``0``. The state will be in the
        :class:`~cocotb.triggers.ReadOnly` phase so sim will need
        to move to :class:`~cocotb.triggers.NextTimeStep` before
        registering more callbacks can occur.
        """
        await ReadOnly()
        while signal.value.integer != 0:
            await Edge(signal)
            await ReadOnly()
        await NextTimeStep()

    def __str__(self):
        """Provide the name of the bus"""
        return str(self.name)


class ValidatedBusDriver(BusDriver):
    """Same as a :class:`BusDriver` except we support an optional generator
    to control which cycles are valid.

    Args:
        entity (SimHandle): A handle to the simulator entity.
        name (str): Name of this bus.
        clock (SimHandle): A handle to the clock associated with this bus.
        valid_generator (generator, optional): a generator that yields tuples of
            ``(valid, invalid)`` cycles to insert.
    """

    def __init__(
        self, entity: SimHandleBase, name: str, clock: SimHandleBase, *,
        valid_generator: Iterable[Tuple[int, int]] = None, **kwargs: Any
    ) -> None:
        BusDriver.__init__(self, entity, name, clock, **kwargs)
        self.on = None
        self.off = None
        # keep this line after the on/off attributes since it overwrites them
        self.set_valid_generator(valid_generator=valid_generator)

    def _next_valids(self):
        """Optionally insert invalid cycles every N cycles.

        The generator should yield tuples with the number of cycles to be
        on followed by the number of cycles to be off.
        The ``on`` cycles should be non-zero, we skip invalid generator entries.
        """
        self.on = False

        if self.valid_generator is not None:
            while not self.on:
                try:
                    self.on, self.off = next(self.valid_generator)
                except StopIteration:
                    # If the generator runs out stop inserting non-valid cycles
                    self.on = True
                    self.log.info("Valid generator exhausted, not inserting "
                                  "non-valid cycles anymore")
                    return

            self.log.debug("Will be on for %d cycles, off for %s" %
                           (self.on, self.off))
        else:
            # Valid every clock cycle
            self.on, self.off = True, False
            self.log.debug("Not using valid generator")

    def set_valid_generator(self, valid_generator=None):
        """Set a new valid generator for this bus."""
        self.valid_generator = valid_generator
        self._next_valids()


@coroutine
async def polled_socket_attachment(driver, sock):
    """Non-blocking socket attachment that queues any payload received from the
    socket to be queued for sending into the driver.
    """
    import socket
    import errno
    sock.setblocking(False)
    driver.log.info("Listening for data from %s" % repr(sock))
    while True:
        await RisingEdge(driver.clock)
        try:
            data = sock.recv(4096)
        except socket.error as e:
            if e.args[0] in [errno.EAGAIN, errno.EWOULDBLOCK]:
                continue
            else:
                driver.log.error(repr(e))
                raise
        if not len(data) > 0:
            driver.log.info("Remote end closed the connection")
            break
        driver.append(data)
