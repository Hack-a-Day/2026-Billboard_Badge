"""PocketReader-owned B/W display session using the stock badge driver."""

import asyncio as aio
import sys

from lib.epd import display


class ReaderDisplay:
    def __init__(self):
        self.panel = None
        self.busy = False
        self.error = None
        self.small_updates = 0
        self.partial_mode = False
        self.quick_boot = False

    def ready(self):
        return not self.busy and display.is_done_updating()

    async def _wait_stock(self):
        while not display.is_done_updating():
            await aio.sleep_ms(25)

    def queue_frame(self, partial=False):
        if self.busy:
            raise RuntimeError("PocketReader display is busy")
        self.busy = True
        aio.create_task(self._show_frame(partial))

    async def _show_frame(self, partial=False):
        try:
            if self.panel is None:
                if not self.quick_boot:
                    # Preserve the drawn frame during the normal color clear.
                    frame = bytes(display._framebuffer_black)
                    display.clear()
                    display.update()
                    await self._wait_stock()
                    display._framebuffer_black[:] = frame
                from lib.pocketreader_partial import PartialEPD

                panel = PartialEPD()
                # Quick Boot uses a black/white wipe instead of the color clear.
                # Red left by another app can remain until a normal refresh.
                await panel.start(clean_red=False, known_white=not self.quick_boot)
                self.panel = panel
                partial = False
            self.panel.buf[:] = display._framebuffer_black
            if partial != self.partial_mode:
                from lib.pocketreader_partial import partial_table, still_table
                self.panel._lut = partial_table() if partial else still_table()
                await self.panel.restart()
                self.partial_mode = partial
            if partial:
                await self.panel.show()
                self.small_updates += 1
            else:
                await self.panel.show_still()
                self.small_updates = 0
            self.error = None
        except Exception as exc:
            self.error = str(exc)
            sys.print_exception(exc)
            if self.panel is not None:
                try:
                    await self.panel.stop()
                finally:
                    self.panel = None
                    self.partial_mode = False
            # Retain a usable stock full-refresh fallback after an error.
            display.update()
            await self._wait_stock()
        finally:
            self.busy = False

    async def close(self):
        while self.busy:
            await aio.sleep_ms(25)
        if self.panel is not None:
            panel = self.panel
            self.panel = None
            self.partial_mode = False
            await panel.stop()

    def queue_sleep_image(self, path):
        if self.busy:
            raise RuntimeError("PocketReader display is busy")
        self.busy = True
        aio.create_task(self._show_sleep_image(path))

    async def _show_sleep_image(self, path):
        try:
            if not self.quick_boot and self.panel is not None:
                panel = self.panel
                self.panel = None
                self.partial_mode = False
                await panel.stop()
            display.clear()
            display.draw_png(path)
            if self.quick_boot:
                # Red cannot be driven in Quick Boot mode. Dither those pixels
                # to a black-and-white checkerboard so they appear gray.
                black = display._framebuffer_black
                red = display._framebuffer_red
                row_bytes = 960 // 8
                for index in range(len(red)):
                    pattern = 0xaa if (index // row_bytes) % 2 == 0 else 0x55
                    black[index] |= red[index] & pattern
                    red[index] = 0
                if self.panel is None:
                    from lib.pocketreader_partial import PartialEPD
                    self.panel = PartialEPD()
                    await self.panel.start(clean_red=False, known_white=False)
                    self.partial_mode = False
                self.panel.buf[:] = black
                if self.partial_mode:
                    from lib.pocketreader_partial import still_table
                    self.panel._lut = still_table()
                    await self.panel.restart()
                    self.partial_mode = False
                await self.panel.show_still()
                panel = self.panel
                self.panel = None
                await panel.stop()
            else:
                display.update()
                await self._wait_stock()
            self.error = None
        except Exception as exc:
            self.error = str(exc)
            sys.print_exception(exc)
            if self.panel is not None:
                panel = self.panel
                self.panel = None
                self.partial_mode = False
                try:
                    await panel.stop()
                except Exception:
                    pass
        finally:
            self.busy = False
