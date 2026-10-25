"""Behavioural tests for the heartbeat log helper."""

import asyncio
import logging

import pytest

from ump.core.utils.heartbeat import heartbeat

LOGGER_NAME = "test.heartbeat"


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_logs_while_block_is_running(self, caplog):
        # Arrange
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        log = logging.getLogger(LOGGER_NAME)

        # Act
        async with heartbeat(log, "[x] working", interval=0.01):
            await asyncio.sleep(0.1)

        # Assert
        assert any("[x] working — still running" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_silent_when_block_finishes_before_interval(self, caplog):
        # Arrange
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        log = logging.getLogger(LOGGER_NAME)

        # Act
        async with heartbeat(log, "[x] working", interval=10.0):
            pass

        # Assert
        assert caplog.records == []

    @pytest.mark.asyncio
    async def test_stops_logging_after_block_exits(self, caplog):
        # Arrange
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        log = logging.getLogger(LOGGER_NAME)
        async with heartbeat(log, "[x] working", interval=0.01):
            await asyncio.sleep(0.05)
        count = len(caplog.records)

        # Act
        await asyncio.sleep(0.05)

        # Assert
        assert len(caplog.records) == count
