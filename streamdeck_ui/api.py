"""Defines the Python API for interacting with the StreamDeck Configuration UI"""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from functools import partial
from pathlib import Path
from typing import Any

from PIL import Image
from PIL.ImageQt import toqimage
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage, QPixmap
from StreamDeck.Devices import StreamDeck
from StreamDeck.Transport.Transport import TransportError

from streamdeck_ui.config import (
    DEFAULT_BACKGROUND_COLOR,
    DEFAULT_FONT,
    DEFAULT_FONT_COLOR,
    DEFAULT_FONT_SIZE,
    FONTS_PATH,
    STATE_FILE,
    read_state_from_config,
    write_state_to_config,
)
from streamdeck_ui.dimmer import Dimmer
from streamdeck_ui.display.background_color_filter import BackgroundColorFilter
from streamdeck_ui.display.display_grid import DisplayGrid
from streamdeck_ui.display.filter import Filter
from streamdeck_ui.display.image_filter import ImageFilter
from streamdeck_ui.display.text_filter import TextFilter
from streamdeck_ui.logger import logger
from streamdeck_ui.model import ButtonMultiState, ButtonState, DeckState
from streamdeck_ui.plugin_system.plugin_manager import PluginManager
from streamdeck_ui.stream_deck_monitor import StreamDeckMonitor


class KeySignalEmitter(QObject):
    key_pressed = Signal(str, int, bool)


class StreamDeckSignalEmitter(QObject):
    attached = Signal(dict)
    "A signal that is raised whenever a new StreamDeck is attached."
    detached = Signal(str)
    "A signal that is raised whenever a StreamDeck is detached. "
    cpu_changed = Signal(str, int)


class StreamDeckServer:
    """A StreamDeckServer represents the core server logic for interacting and
    managing multiple Stream Decks.
    """

    decks_by_serial: dict[str, StreamDeck.StreamDeck] = {}
    "Lookup with serial number -> StreamDeck"

    decks_map_id_to_serial: dict[str, str] = {}
    "Lookup with device.id -> serial number"

    state: dict[str, DeckState] = {}
    "The data structure holding configuration for all Stream Decks by serial number"

    key_event_lock: threading.Lock
    "Lock to serialize key press events"

    lock: threading.Lock = threading.Lock()
    "Lock to coordinate polling, updates etc to Stream Decks"

    display_handlers: dict[str, DisplayGrid] = {}
    "Lookup with serial number for each Stream Deck display handler"

    dimmers: dict[str, Dimmer] = {}
    "Lookup with serial number for each Stream Deck dimmer"

    monitor: StreamDeckMonitor | None = None
    "Monitors for Stream Deck(s) attached to the computer"

    plugin_manager: PluginManager | None = None
    "Plugin manager for handling plugins"

    plugevents = StreamDeckSignalEmitter()
    "Use the connect method on the attached and detached methods to subscribe"

    streamdeck_keys = KeySignalEmitter()
    "Use the connect method on the key_pressed signal to subscribe"

    def __init__(self) -> None:
        self.decks_by_serial: dict[str, StreamDeck.StreamDeck] = {}

        # REVIEW: Should we use the same lock as the display? What exactly
        # are we protecting? The UI is signaled via message passing.
        self.key_event_lock = threading.Lock()
        self.display_handlers: dict[str, DisplayGrid] = {}

        self.lock: threading.Lock = threading.Lock()
        self.dimmers: dict[str, Dimmer] = {}

        # REVIEW: Should we just create one signal emitter for
        # plug events and key signals?
        self.streamdeck_keys = KeySignalEmitter()
        self.plugevents = StreamDeckSignalEmitter()

        # Initialize plugin manager
        plugins_dir = Path.home() / ".streamdeck_ui" / "plugins"
        self.plugin_manager = PluginManager(plugins_dir)
        self.plugin_manager.discover_plugins()

        # Start plugin monitor thread
        self.plugin_monitor_thread = threading.Thread(target=self.plugin_manager.monitor_instances, daemon=True)
        self.plugin_monitor_thread.start()

    def shutdown(self) -> None:
        """Shutdown the StreamDeck server and cleanup all resources."""
        logger.info("Shutting down StreamDeck server...")

        # Stop all plugin instances in parallel
        if self.plugin_manager:
            logger.info("Stopping all plugin instances...")
            instance_ids = list(self.plugin_manager.instances.keys())
            if instance_ids:
                def _stop_instance(instance_id):
                    try:
                        self.plugin_manager.stop_instance(instance_id)
                    except Exception as e:
                        logger.error(f"Error stopping plugin instance {instance_id}: {e}")

                with ThreadPoolExecutor(max_workers=len(instance_ids)) as executor:
                    executor.map(_stop_instance, instance_ids)

        # Stop dimmers
        for serial_number in list(self.dimmers.keys()):
            try:
                self.stop_dimmer(serial_number)
            except Exception as e:
                logger.error(f"Error stopping dimmer for {serial_number}: {e}")

        # Close display handlers
        for serial_number, display_handler in list(self.display_handlers.items()):
            try:
                display_handler.stop()
            except Exception as e:
                logger.error(f"Error stopping display handler for {serial_number}: {e}")

        logger.info("Shutdown complete")

    def stop_dimmer(self, serial_number: str) -> None:
        """Stops the dimmer for the given Stream Deck

        :param serial_number: The Stream Deck serial number.
        :type serial_number: str
        """
        self.dimmers[serial_number].stop()

    def reset_dimmer(self, serial_number: str) -> bool:
        """Resets the dimmer for the given Stream Deck. This means the display
        will not be dimmed and the timer starts. Reloads configuration.

        Args:
            serial_number (str): The Stream Deck serial number
        Returns:
            bool: Returns True if the dimmer had to be reset (i.e. woken up), False otherwise.
        """
        self.dimmers[serial_number].brightness = self.get_brightness(serial_number)
        self.dimmers[serial_number].brightness_dimmed = self.get_brightness_dimmed(serial_number)
        return self.dimmers[serial_number].reset()

    def toggle_dimmers(self):
        """If at least one Deck is still "on", all will be dimmed off. Otherwise,
        toggles displays on.
        """
        at_least_one = False
        for _serial_number, dimmer in self.dimmers.items():
            if not dimmer.dimmed:
                at_least_one = True
                break

        for _serial_number, dimmer in self.dimmers.items():
            if at_least_one:
                dimmer.dim()
            else:
                dimmer.dim(True)

    def _cpu_usage_callback(self, serial_number: str, cpu_usage: int):
        """An internal method that takes emits a signal on a QObject.

        :param serial_number: The Stream Deck serial number
        :type serial_number: str
        :param cpu_usage: The current CPU usage
        :type cpu_usage: int
        """
        self.plugevents.cpu_changed.emit(serial_number, cpu_usage)

    def _key_change_callback(self, serial_number: str, _deck: StreamDeck.StreamDeck, key: int, state: bool) -> None:
        """Callback whenever a key is pressed.

        Stream Deck key events fire on a background thread. Emit a signal
        to bring it back to UI thread, so we can use Qt objects for timers etc.
        Since multiple keys could fire simultaneously, we need to protect
        shared state with a lock
        """
        with self.key_event_lock:
            self.display_handlers[serial_number].set_keypress(key, state)
            self.streamdeck_keys.key_pressed.emit(serial_number, key, state)

    def get_display_timeout(self, serial_number: str) -> int:
        """Returns the amount of time in seconds before the display gets dimmed."""
        if serial_number not in self.state:
            return 0
        return self.state[serial_number].display_timeout

    def set_display_timeout(self, serial_number: str, timeout: int) -> None:
        """Sets the amount of time in seconds before the display gets dimmed."""
        if serial_number not in self.state:
            return

        if self.state[serial_number].display_timeout == timeout:
            return

        self.state[serial_number].display_timeout = timeout
        self.dimmers[serial_number].timeout = timeout

        self._save_state()

    def _save_state(self):
        self.export_config(STATE_FILE)

    def open_config(self, config_file: str):
        self.state = read_state_from_config(config_file)

    def import_config(self, config_file: str) -> None:
        self.stop()
        self.open_config(config_file)
        self._save_state()
        self.start()

    def export_config(self, output_file: str) -> None:
        write_state_to_config(output_file, self.state)

    def _on_steam_deck_attached(self, streamdeck_id: str, streamdeck: StreamDeck):
        streamdeck.open()
        streamdeck.reset()
        serial_number = streamdeck.get_serial_number()

        self.decks_map_id_to_serial[streamdeck_id] = serial_number
        self.decks_by_serial[serial_number] = streamdeck

        self.set_default_state(serial_number, streamdeck.deck_type())
        self._initialize_stream_deck_page_state(serial_number, 0, streamdeck.key_count())

        streamdeck.set_key_callback(partial(self._key_change_callback, serial_number))
        self._update_streamdeck_filters(serial_number)

        self.dimmers[serial_number] = Dimmer(
            self.get_display_timeout(serial_number),
            self.get_brightness(serial_number),
            self.get_brightness_dimmed(serial_number),
            lambda brightness: self.decks_by_serial[serial_number].set_brightness(brightness),
        )
        self.dimmers[serial_number].reset()

        self.plugevents.attached.emit(
            {
                "id": streamdeck_id,
                "serial_number": serial_number,
                "type": streamdeck.deck_type(),
                "layout": streamdeck.key_layout(),
            }
        )

        # Restore plugins in background so UI is not blocked
        threading.Thread(
            target=self._restore_plugins_for_deck, args=(serial_number,), daemon=True
        ).start()

    def set_default_state(self, serial_number: str, deck_type: str):
        if serial_number in self.state:
            return
        elif deck_type in self.state:
            logger.info(f"no configuration found for {serial_number}, use generic configuration for type: {deck_type}.")
            self.state[serial_number] = deepcopy(self.state[deck_type])

    def _initialize_stream_deck_page_state(self, serial_number: str, page: int, key_count: int):
        """Initializes the state for the given serial number. This allocates
        buttons and pages based on the layout.

        :param serial_number: The Stream Deck serial number
        :type serial_number: str
        :param page: The page of the Stream Deck
        :type page: int
        :param key_count: The total number of buttons on the Stream Deck
        :type key_count: int
        """
        self.state[serial_number] = self.state.setdefault(serial_number, DeckState())
        for button in range(key_count):
            self._button_state(serial_number, page, button)

    def add_new_page(self, serial_number: str):
        """Adds a new page to the Stream Deck

        :param serial_number: The Stream Deck serial number
        :type serial_number: str
        :return: The new page index
        :rtype: int
        """
        pages = self.get_pages(serial_number)
        new_page_index = self._calculate_new_index(pages)
        self._initialize_stream_deck_page_state(
            serial_number, new_page_index, self.decks_by_serial[serial_number].key_count()
        )
        self.display_handlers[serial_number].initialize_page(new_page_index)
        self.display_handlers[serial_number].synchronize()

        return new_page_index

    @staticmethod
    def _calculate_new_index(items: list[int]) -> int:
        """Calculates the next free index for a list of items"""
        items_set = set(items)
        max_item = max(items) if items else 0

        for item_index in range(1, max_item + 2):
            if item_index not in items_set:
                return item_index
        return max_item + 2

    def remove_page(self, serial_number: str, page: int):
        """Removes a page from the Stream Deck

        :param serial_number: The Stream Deck serial number
        :type serial_number: str
        :param page: The page index
        :type page: int
        """
        if len(self.get_pages(serial_number)) == 1:
            return

        del self.state[serial_number].buttons[page]
        self.display_handlers[serial_number].remove_page(page)

    def _on_steam_deck_detached(self, deck_id: str):
        serial_number = self.decks_map_id_to_serial.get(deck_id, None)
        if serial_number:
            self._cleanup(deck_id, serial_number)
            self.plugevents.detached.emit(serial_number)

    def _cleanup(self, deck_id: str, serial_number: str):
        display_grid = self.display_handlers[serial_number]
        display_grid.stop()
        del self.display_handlers[serial_number]

        dimmer = self.dimmers[serial_number]
        dimmer.stop()
        del self.dimmers[serial_number]

        streamdeck = self.decks_by_serial[serial_number]
        try:
            if streamdeck.connected():
                streamdeck.set_brightness(50)
                streamdeck.reset()
                streamdeck.close()
        except TransportError:
            pass

        del self.decks_by_serial[serial_number]
        del self.decks_map_id_to_serial[deck_id]

    def start(self):
        if not self.monitor:
            self.monitor = StreamDeckMonitor(self.lock, self._on_steam_deck_attached, self._on_steam_deck_detached)
        self.monitor.start()

    def stop(self):
        self.monitor.stop()

    def get_deck_layout(self, serial_number: str) -> tuple[int, int]:
        """Returns a tuple containing the number of rows and columns for the specified Stream Deck"""
        return self.decks_by_serial[serial_number].key_layout()

    def _button_state(self, serial_number: str, page: int, button: int, state: int | None = None) -> ButtonState:
        multi_state = self._button_multi_state(serial_number, page, button)
        # if no state is specified, use the current state
        choose_state = state or multi_state.state
        # if the choose state is not in the states dict, add it
        multi_state.states[choose_state] = multi_state.states.setdefault(choose_state, ButtonState())
        return multi_state.states[choose_state]

    def get_button_state_object(self, serial_number: str, page: int, button: int, state: int) -> ButtonState:
        """Returns the ButtonState object for the given button"""
        return self._button_state(serial_number, page, button, state)

    def _button_multi_state(self, serial_number: str, page: int, button: int) -> ButtonMultiState:
        """Returns the ButtonMultiState for the given button"""
        # if the page is not in the pages dict, add it
        self.state[serial_number].buttons[page] = self.state[serial_number].buttons.setdefault(page, {})
        # if the button is not in the buttons dict, add it with a default state
        self.state[serial_number].buttons[page][button] = (
            self.state[serial_number]
            .buttons[page]
            .setdefault(button, ButtonMultiState(state=0, states={0: ButtonState()}))
        )
        return self.state[serial_number].buttons[page][button]

    def get_button_state(self, serial_number: str, page: int, button: int) -> int:
        """Returns the state of a button"""
        return self._button_multi_state(serial_number, page, button).state

    def get_button_states(self, serial_number: str, page: int, button: int) -> list[int]:
        """Returns the states of a button"""
        return sorted(list(self._button_multi_state(serial_number, page, button).states.keys()))

    def add_new_button_state(self, serial_number: str, page: int, button: int) -> int:
        """Adds a new button state"""
        states = self.get_button_states(serial_number, page, button)
        new_button_state_index = self._calculate_new_index(states)
        self._button_multi_state(serial_number, page, button).states[new_button_state_index] = ButtonState()
        return new_button_state_index

    def remove_button_state(self, serial_number: str, page: int, button: int, state: int) -> None:
        """Removes a button state"""
        if len(self.get_button_states(serial_number, page, button)) == 1:
            return
        del self._button_multi_state(serial_number, page, button).states[state]

    def set_button_state(self, serial_number: str, page: int, button: int, state: int) -> None:
        """Sets the state of a button"""
        if self.get_button_state(serial_number, page, button) != state:
            states = self.get_button_states(serial_number, page, button)
            if state in states:
                self._button_multi_state(serial_number, page, button).state = state
                self._save_state()
                self._update_button_filters(serial_number, page, button)
                display_handler = self.display_handlers[serial_number]
                display_handler.synchronize()

    def get_button_switch_state(self, serial_number: str, page: int, button: int) -> int:
        """Returns the state switch set for the specified button. 0 implies no state switch."""
        return self._button_state(serial_number, page, button).switch_state

    def set_button_switch_state(self, serial_number: str, page: int, button: int, switch_state: int) -> None:
        """Sets the state switch associated with the button"""
        if self.get_button_switch_state(serial_number, page, button) != switch_state:
            self._button_state(serial_number, page, button).switch_state = switch_state
            self._save_state()

    def swap_buttons(self, serial_number: str, page: int, source_button: int, target_button: int) -> None:
        """Swaps the properties of the source and target buttons"""
        temp = self.state[serial_number].buttons[page][source_button]
        self.state[serial_number].buttons[page][source_button] = self.state[serial_number].buttons[page][target_button]
        self.state[serial_number].buttons[page][target_button] = temp
        self._save_state()

        # Update rendering for these two images
        self._update_button_filters(serial_number, page, source_button)
        self._update_button_filters(serial_number, page, target_button)
        display_handler = self.display_handlers[serial_number]
        display_handler.synchronize()

    def is_button_empty(self, serial_number: str, page: int, button: int) -> bool:
        """Returns True if a button has no configuration (all default values)"""
        multi_state = self._button_multi_state(serial_number, page, button)
        for state in multi_state.states.values():
            if state != ButtonState():
                return False
        return True

    def find_first_free_button(self, serial_number: str, page: int) -> int | None:
        """Returns the index of the first empty button on the given page, or None if all are occupied"""
        key_count = self.decks_by_serial[serial_number].key_count()
        for button_index in range(key_count):
            if self.is_button_empty(serial_number, page, button_index):
                return button_index
        return None

    def move_button_to_page(self, serial_number: str, source_page: int, button_index: int, target_page: int) -> bool:
        """Moves a button to the first free slot on the target page. Returns True on success."""
        target_button = self.find_first_free_button(serial_number, target_page)
        if target_button is None:
            return False

        # Copy button data to target
        source_data = self.state[serial_number].buttons[source_page][button_index]
        self.state[serial_number].buttons[target_page][target_button] = source_data

        # Reset source button to default
        self.state[serial_number].buttons[source_page][button_index] = ButtonMultiState(
            state=0, states={0: ButtonState()}
        )
        self._save_state()

        # Update rendering for both pages
        self._update_button_filters(serial_number, source_page, button_index)
        self._update_button_filters(serial_number, target_page, target_button)
        display_handler = self.display_handlers[serial_number]
        display_handler.synchronize()
        return True

    def set_button_text(self, deck_id: str, page: int, button: int, text: str) -> None:
        """Set the text associated with a button"""
        if self.get_button_text(deck_id, page, button) != text:
            self._button_state(deck_id, page, button).text = text
            self._save_state()
            self._update_button_filters(deck_id, page, button)
            display_handler = self.display_handlers[deck_id]
            display_handler.synchronize()

    def get_button_text(self, deck_id: str, page: int, button: int) -> str:
        """Returns the text set for the specified button"""
        return self._button_state(deck_id, page, button).text

    def set_button_icon(self, deck_id: str, page: int, button: int, icon: str) -> None:
        """Sets the icon associated with a button"""
        if self.get_button_icon(deck_id, page, button) != icon:
            self._button_state(deck_id, page, button).icon = icon
            self._save_state()

            self._update_button_filters(deck_id, page, button)
            display_handler = self.display_handlers[deck_id]
            display_handler.synchronize()

    def get_button_text_vertical_align(self, serial_number: str, page: int, button: int) -> str:
        """Gets the vertical text alignment. Values are bottom, middle-bottom, middle, middle-top, top"""
        return self._button_state(serial_number, page, button).text_vertical_align

    def get_button_text_horizontal_align(self, serial_number: str, page: int, button: int) -> str:
        """Gets the horizontal text alignment. Values are left, center, right"""
        return self._button_state(serial_number, page, button).text_horizontal_align

    def set_button_text_horizontal_align(self, serial_number: str, page: int, button: int, alignment: str) -> None:
        """Gets the horizontal text alignment. Values are left, center, right"""
        if self.get_button_text_horizontal_align(serial_number, page, button) != alignment:
            self._button_state(serial_number, page, button).text_horizontal_align = alignment
            self._save_state()
            self._update_button_filters(serial_number, page, button)
            display_handler = self.display_handlers[serial_number]
            display_handler.synchronize()

    def set_button_text_vertical_align(self, serial_number: str, page: int, button: int, alignment: str) -> None:
        """Gets the vertical text alignment. Values are bottom, middle-bottom, middle, middle-top, top"""
        if self.get_button_text_vertical_align(serial_number, page, button) != alignment:
            self._button_state(serial_number, page, button).text_vertical_align = alignment
            self._save_state()
            self._update_button_filters(serial_number, page, button)
            display_handler = self.display_handlers[serial_number]
            display_handler.synchronize()

    def set_button_font_color(self, serial_number: str, page: int, button: int, color: str) -> None:
        """Sets the text color associated with a button"""
        if self.get_button_font_color(serial_number, page, button) != color:
            # Don't pollute .streamdeck_ui.json with entries of the default value
            if color == DEFAULT_FONT_COLOR:
                color = ""
            self._button_state(serial_number, page, button).font_color = color
            self._save_state()
            self._update_button_filters(serial_number, page, button)

            try:
                display_handler = self.display_handlers[serial_number]
                display_handler.synchronize()
            except KeyError:
                raise ValueError(f"Invalid serial number: {serial_number}")

    def get_button_font_color(self, serial_number: str, page: int, button: int) -> str:
        """Returns the text color set for the specified button"""
        return self._button_state(serial_number, page, button).font_color

    def set_button_background_color(self, serial_number: str, page: int, button: int, color: str) -> None:
        """Sets the background color associated with a button"""
        if self.get_button_background_color(serial_number, page, button) != color:
            # Don't pollute .streamdeck_ui.json with entries of the default value
            if color == DEFAULT_BACKGROUND_COLOR:
                color = ""
            self._button_state(serial_number, page, button).background_color = color
            self._save_state()
            self._update_button_filters(serial_number, page, button)

            try:
                display_handler = self.display_handlers[serial_number]
                display_handler.synchronize()
            except KeyError:
                raise ValueError(f"Invalid serial number: {serial_number}")

    def get_button_background_color(self, serial_number: str, page: int, button: int) -> str:
        """Returns the background color set for the specified button"""
        return self._button_state(serial_number, page, button).background_color

    def get_button_icon_pixmap(self, serial_number: str, page: int, button: int) -> QPixmap | None:
        """Returns the QPixmap value for the given button (streamdeck, page, button)"""
        pil_image = self.display_handlers[serial_number].get_image(page, button)
        if pil_image:
            qt_image = toqimage(pil_image)
            qt_image = qt_image.convertToFormat(QImage.Format.Format_ARGB32)
            return QPixmap(qt_image)
        return None

    def get_button_icon(self, serial_number: str, page: int, button: int) -> str:
        """Returns the icon path for the specified button"""
        return self._button_state(serial_number, page, button).icon

    def set_button_change_brightness(self, serial_number: str, page: int, button: int, amount: int) -> None:
        """Sets the brightness changing associated with a button"""
        if self.get_button_change_brightness(serial_number, page, button) != amount:
            self._button_state(serial_number, page, button).brightness_change = amount
            self._save_state()

    def get_button_change_brightness(self, serial_number: str, page: int, button: int) -> int:
        """Returns the brightness change set for a particular button"""
        return self._button_state(serial_number, page, button).brightness_change

    def set_button_command(self, serial_number: str, page: int, button: int, command: str) -> None:
        """Sets the command associated with the button"""
        if self.get_button_command(serial_number, page, button) != command:
            self._button_state(serial_number, page, button).command = command
            self._save_state()

    def get_button_command(self, serial_number: str, page: int, button: int) -> str:
        """Returns the command set for the specified button"""
        return self._button_state(serial_number, page, button).command

    def set_button_switch_page(self, serial_number: str, page: int, button: int, switch_page: int) -> None:
        """Sets the page switch associated with the button"""
        if self.get_button_switch_page(serial_number, page, button) != switch_page:
            self._button_state(serial_number, page, button).switch_page = switch_page
            self._save_state()

    def get_button_switch_page(self, serial_number: str, page: int, button: int) -> int:
        """Returns the page switch set for the specified button. 0 implies no page switch."""
        return self._button_state(serial_number, page, button).switch_page

    def set_button_temp_switch_page(self, serial_number: str, page: int, button: int, temp_switch_page: int) -> None:
        """Sets the temporary page switch associated with the button"""
        if self.get_button_temp_switch_page(serial_number, page, button) != temp_switch_page:
            self._button_state(serial_number, page, button).temp_switch_page = temp_switch_page
            self._save_state()

    def get_button_temp_switch_page(self, serial_number: str, page: int, button: int) -> int:
        """Returns the temporary page switch set for the specified button. 0 implies no temporary page switch."""
        return self._button_state(serial_number, page, button).temp_switch_page

    def set_button_keys(self, serial_number: str, page: int, button: int, keys: str) -> None:
        """Sets the keys associated with the button"""
        if self.get_button_keys(serial_number, page, button) != keys:
            self._button_state(serial_number, page, button).keys = keys
            self._save_state()

    def set_button_font(self, serial_number: str, page: int, button: int, font: str) -> None:
        if self.get_button_font(serial_number, page, button) != font:
            # Don't pollute .streamdeck_ui.json with entries of the default value
            if font.endswith(DEFAULT_FONT):
                font = ""
            self._button_state(serial_number, page, button).font = font
            self._save_state()
            self._update_button_filters(serial_number, page, button)
            display_handler = self.display_handlers[serial_number]
            display_handler.synchronize()

    def get_button_font_size(self, serial_number: str, page: int, button: int) -> int:
        """Returns the font size set for the specified button"""
        return self._button_state(serial_number, page, button).font_size

    def set_button_font_size(self, serial_number: str, page: int, button: int, font_size: int) -> None:
        if self.get_button_font_size(serial_number, page, button) != font_size:
            # Don't pollute .streamdeck_ui.json with entries of the default value
            if font_size == DEFAULT_FONT_SIZE:
                font_size = 0
            self._button_state(serial_number, page, button).font_size = font_size
            self._save_state()
            self._update_button_filters(serial_number, page, button)
            display_handler = self.display_handlers[serial_number]
            display_handler.synchronize()

    def get_button_keys(self, serial_number: str, page: int, button: int) -> str:
        """Returns the keys set for the specified button"""
        return self._button_state(serial_number, page, button).keys

    def get_button_font(self, serial_number: str, page: int, button: int) -> str:
        """Returns the font set for the specified button"""
        return self._button_state(serial_number, page, button).font

    def set_button_write(self, serial_number: str, page: int, button: int, write: str) -> None:
        """Sets the text meant to be written when button is pressed"""
        if self.get_button_write(serial_number, page, button) != write:
            self._button_state(serial_number, page, button).write = write
            self._save_state()

    def get_button_write(self, serial_number: str, page: int, button: int) -> str:
        """Returns the text to be produced when the specified button is pressed"""
        return self._button_state(serial_number, page, button).write

    def set_button_force_refresh(self, serial_number: str, page: int, button: int, force_refresh: bool) -> None:
        """Sets whether to force icon refresh after command execution"""
        if self.get_button_force_refresh(serial_number, page, button) != force_refresh:
            self._button_state(serial_number, page, button).force_refresh = force_refresh
            self._save_state()

    def get_button_force_refresh(self, serial_number: str, page: int, button: int) -> bool:
        """Returns whether icon refresh is forced after command execution"""
        return self._button_state(serial_number, page, button).force_refresh

    def set_brightness(self, serial_number: str, brightness: int) -> None:
        """Sets the brightness for every button on the deck"""
        if self.get_brightness(serial_number) != brightness:
            self.decks_by_serial[serial_number].set_brightness(brightness)
            self.state[serial_number].brightness = brightness
            self._save_state()

    def get_brightness(self, serial_number: str) -> int:
        """Gets the brightness that is set for the specified stream deck"""
        return self.state[serial_number].brightness

    def get_brightness_dimmed(self, serial_number: str) -> int:
        """Gets the percentage value of the full brightness that is used when dimming the specified
        stream deck"""
        return self.state[serial_number].brightness_dimmed

    def set_brightness_dimmed(self, serial_number: str, brightness_dimmed: int) -> None:
        """Sets the percentage value that will be used for dimming the full brightness"""
        self.state[serial_number].brightness_dimmed = brightness_dimmed
        self._save_state()

    def change_brightness(self, deck_id: str, amount: int = 1) -> None:
        """Change the brightness of the deck by the specified amount"""
        brightness = max(min(self.get_brightness(deck_id) + amount, 100), 0)
        self.set_brightness(deck_id, brightness)
        self.dimmers[deck_id].brightness = brightness
        self.dimmers[deck_id].reset()

    def get_pages(self, serial_number: str) -> list[int]:
        """Returns pages for the specified stream deck"""
        return sorted(list(self.state[serial_number].buttons.keys()))

    def get_page(self, serial_number: str) -> int:
        """Gets the current page shown on the stream deck"""
        return self.state[serial_number].page

    def set_page(self, serial_number: str, page: int) -> None:
        """Sets the current page shown on the stream deck"""
        if self.get_page(serial_number) != page:
            if page not in self.get_pages(serial_number):
                return
            self.state[serial_number].page = page
            self._save_state()

        display_handler = self.display_handlers[serial_number]

        # Let the display know to process new set of pipelines
        display_handler.set_page(page)
        # Wait for at least one cycle
        display_handler.synchronize()

    def set_temp_page(self, serial_number: str, page: int) -> None:
        """Sets a temporary page, storing the current page to return to on release"""
        if page not in self.get_pages(serial_number):
            return
        # Store current page as previous page
        self.state[serial_number].previous_page = self.get_page(serial_number)
        # Switch to temporary page
        self.state[serial_number].page = page

        display_handler = self.display_handlers[serial_number]
        display_handler.set_page(page)
        display_handler.synchronize()

    def restore_previous_page(self, serial_number: str) -> None:
        """Restores the previous page after temporary page switch"""
        previous_page = self.state[serial_number].previous_page
        if previous_page in self.get_pages(serial_number):
            self.state[serial_number].page = previous_page
            self.state[serial_number].previous_page = 0

            display_handler = self.display_handlers[serial_number]
            display_handler.set_page(previous_page)
            display_handler.synchronize()

    def _update_streamdeck_filters(self, serial_number: str):
        """Updates the filters for all the StreamDeck buttons.

        :param serial_number: The StreamDeck serial number.
        :type serial_number: str
        """

        # if deck is not attached then do nothing
        if serial_number not in self.decks_by_serial:
            return

        pages = self.get_pages(serial_number)
        display_handler = self.display_handlers.get(
            serial_number, DisplayGrid(self.lock, self.decks_by_serial[serial_number], pages, self._cpu_usage_callback)
        )
        display_handler.set_page(self.get_page(serial_number))
        self.display_handlers[serial_number] = display_handler

        for page, buttons in self.state[serial_number].buttons.items():
            for button in buttons:
                self._update_button_filters(serial_number, page, button)

        display_handler.start()

    def _update_button_filters(self, serial_number: str, page: int, button: int):
        """Sets the filters for a given button. Any previous filters are replaced.

        :param serial_number: The StreamDeck serial number
        :type serial_number: str
        :param page: The page number
        :type page: int
        :param button: The button to update
        :type button: int
        """
        display_handler = self.display_handlers[serial_number]
        button_settings = self._button_state(serial_number, page, button)
        filters: list[Filter] = []

        background_color = button_settings.background_color or DEFAULT_BACKGROUND_COLOR
        filters.append(BackgroundColorFilter(background_color))

        if button_settings.icon:
            filters.append(ImageFilter(button_settings.icon))

        if button_settings.text:
            font_size = int(button_settings.font_size) if button_settings.font_size else DEFAULT_FONT_SIZE
            font_color = button_settings.font_color or DEFAULT_FONT_COLOR
            font = button_settings.font or DEFAULT_FONT
            # if font is not absolute means a default font, prefix it
            if not font.startswith("/"):
                font = os.path.join(FONTS_PATH, font)
            # add fallback font logic
            filters.append(
                TextFilter(
                    button_settings.text,
                    font,
                    font_size,
                    font_color,
                    button_settings.text_vertical_align,
                    button_settings.text_horizontal_align,
                )
            )

        display_handler.replace(page, button, filters)

    # Plugin API methods

    def _restore_single_plugin(self, serial_number: str, page: int, button: int, state_id: int, button_state: ButtonState) -> None:
        """Restore a single plugin instance. Designed to run in a thread pool."""
        assert self.plugin_manager is not None
        try:
            instance_id = self.plugin_manager.create_instance(
                plugin_id=button_state.plugin_id,
                deck_serial=serial_number,
                page=page,
                button=button,
                config=button_state.plugin_config,
                can_switch_page=button_state.plugin_can_switch_page,
            )

            if instance_id:
                instance = self.plugin_manager.get_instance(instance_id)
                if instance:
                    instance.on_image_update = self._handle_plugin_image_update
                    instance.on_page_switch_request = self._handle_plugin_page_switch
                    instance.on_log_message = self._handle_plugin_log_message

                success = self.plugin_manager.start_instance(instance_id)
                if success:
                    logger.info(f"Successfully restored plugin instance {instance_id}")
                    current_page = self.get_page(serial_number)
                    if current_page == page and instance:
                        instance.send_button_visible()
                else:
                    logger.error(f"Failed to start restored plugin instance {instance_id}")
            else:
                logger.error(f"Failed to create plugin instance for {button_state.plugin_id}")
        except Exception as e:
            logger.error(f"Error restoring plugin {button_state.plugin_id}: {e}", exc_info=True)

    def _restore_plugins_for_deck(self, serial_number: str) -> None:
        """Restore all plugin instances for a given deck from saved state.

        Plugins are started in parallel using a thread pool to avoid
        blocking the UI while waiting for each plugin's socket connection.

        Args:
            serial_number: Stream Deck serial number
        """
        assert self.plugin_manager is not None, "Plugin manager must be initialized"

        if serial_number not in self.state:
            return

        deck_state = self.state[serial_number]

        # Collect all plugins to restore
        plugins_to_restore = []
        for page, buttons in deck_state.buttons.items():
            for button, button_multi_state in buttons.items():
                for state_id, button_state in button_multi_state.states.items():
                    if button_state.plugin_id:
                        logger.info(
                            f"Restoring plugin {button_state.plugin_id} for "
                            f"deck {serial_number}, page {page}, button {button}, state {state_id}"
                        )
                        plugins_to_restore.append((page, button, state_id, button_state))

        if not plugins_to_restore:
            return

        # Start all plugins in parallel
        with ThreadPoolExecutor(max_workers=len(plugins_to_restore)) as executor:
            for page, button, state_id, button_state in plugins_to_restore:
                executor.submit(self._restore_single_plugin, serial_number, page, button, state_id, button_state)

    def get_button_plugin_id(self, serial_number: str, page: int, button: int) -> str:
        """Get the plugin ID for a button."""
        state = self.get_button_state(serial_number, page, button)
        button_settings = self.get_button_state_object(serial_number, page, button, state)
        return button_settings.plugin_id

    def set_button_plugin_id(self, serial_number: str, page: int, button: int, plugin_id: str) -> None:
        """Set the plugin ID for a button."""
        state = self.get_button_state(serial_number, page, button)
        button_settings = self.get_button_state_object(serial_number, page, button, state)
        button_settings.plugin_id = plugin_id
        self._save_state()

    def get_button_plugin_config(self, serial_number: str, page: int, button: int) -> dict[str, Any]:
        """Get the plugin configuration for a button."""
        state = self.get_button_state(serial_number, page, button)
        button_settings = self.get_button_state_object(serial_number, page, button, state)
        return button_settings.plugin_config

    def set_button_plugin_config(self, serial_number: str, page: int, button: int, config: dict[str, Any]) -> None:
        """Set the plugin configuration for a button."""
        assert self.plugin_manager is not None

        state = self.get_button_state(serial_number, page, button)
        button_settings = self.get_button_state_object(serial_number, page, button, state)
        button_settings.plugin_config = config
        self._save_state()

        # Update running plugin instance if exists
        instance_id = f"{button_settings.plugin_id}_{serial_number}_{page}_{button}"
        instance = self.plugin_manager.get_instance(instance_id)
        if instance and instance.running:
            instance.send_config_update(config)

    def get_button_plugin_can_switch_page(self, serial_number: str, page: int, button: int) -> bool:
        """Get whether the plugin can switch pages."""
        state = self.get_button_state(serial_number, page, button)
        button_settings = self.get_button_state_object(serial_number, page, button, state)
        return button_settings.plugin_can_switch_page

    def set_button_plugin_can_switch_page(self, serial_number: str, page: int, button: int, can_switch: bool) -> None:
        """Set whether the plugin can switch pages."""
        state = self.get_button_state(serial_number, page, button)
        button_settings = self.get_button_state_object(serial_number, page, button, state)
        button_settings.plugin_can_switch_page = can_switch
        self._save_state()

    def attach_plugin_to_button(
        self,
        serial_number: str,
        page: int,
        button: int,
        plugin_id: str,
        config: dict[str, Any],
        can_switch_page: bool = False,
    ) -> bool:
        """Attach a plugin to a button and start it.

        Args:
            serial_number: Stream Deck serial number
            page: Page number
            button: Button number
            plugin_id: Plugin identifier
            config: Plugin configuration
            can_switch_page: Whether plugin can switch pages

        Returns:
            True if successful
        """
        assert self.plugin_manager is not None
        # Check if an instance already exists for this button
        instance_id = f"{plugin_id}_{serial_number}_{page}_{button}"
        existing_instance = self.plugin_manager.get_instance(instance_id)

        if existing_instance:
            # Update existing instance configuration
            logger.info(f"Updating existing plugin instance {instance_id}")
            self.set_button_plugin_config(serial_number, page, button, config)
            self.set_button_plugin_can_switch_page(serial_number, page, button, can_switch_page)
            existing_instance.send_config_update(config)
            return True

        # Save plugin info to button state
        self.set_button_plugin_id(serial_number, page, button, plugin_id)
        self.set_button_plugin_config(serial_number, page, button, config)
        self.set_button_plugin_can_switch_page(serial_number, page, button, can_switch_page)

        # Create and start plugin instance
        instance_id_result = self.plugin_manager.create_instance(
            plugin_id=plugin_id,
            deck_serial=serial_number,
            page=page,
            button=button,
            config=config,
            can_switch_page=can_switch_page,
        )

        if not instance_id_result:
            logger.error(f"Failed to create plugin instance for {plugin_id}")
            return False

        # Set up callbacks
        instance = self.plugin_manager.get_instance(instance_id_result)
        if instance:
            instance.on_image_update = self._handle_plugin_image_update
            instance.on_page_switch_request = self._handle_plugin_page_switch
            instance.on_log_message = self._handle_plugin_log_message

        # Start the plugin
        success = self.plugin_manager.start_instance(instance_id_result)
        if not success:
            logger.error(f"Failed to start plugin instance {instance_id_result}")
            return False

        # If button is currently visible, notify plugin
        current_page = self.get_page(serial_number)
        if current_page == page and instance:
            instance.send_button_visible()

        return True

    def detach_plugin_from_button(self, serial_number: str, page: int, button: int) -> None:
        """Detach a plugin from a button and stop it."""
        assert self.plugin_manager is not None
        plugin_id = self.get_button_plugin_id(serial_number, page, button)
        if not plugin_id:
            return

        # Stop and remove plugin instance
        instance_id = f"{plugin_id}_{serial_number}_{page}_{button}"
        self.plugin_manager.remove_instance(instance_id)

        # Clear plugin info from button state
        self.set_button_plugin_id(serial_number, page, button, "")
        self.set_button_plugin_config(serial_number, page, button, {})
        self.set_button_plugin_can_switch_page(serial_number, page, button, False)

    def clear_button(self, serial_number: str, page: int, button: int) -> None:
        """Reset a button to empty: stop any attached plugin and wipe its config.

        Mirrors the "reset source button to default" step of
        :meth:`move_button_to_page`, but in place (no move), and additionally
        stops/detaches a plugin if one is attached.
        """
        if self.plugin_manager is not None and self.get_button_plugin_id(
            serial_number, page, button
        ):
            self.detach_plugin_from_button(serial_number, page, button)

        self.state[serial_number].buttons[page][button] = ButtonMultiState(
            state=0, states={0: ButtonState()}
        )
        self._save_state()
        self._update_button_filters(serial_number, page, button)
        self.display_handlers[serial_number].synchronize()

    def _handle_plugin_image_update(
        self, deck_serial: str, page: int, button: int, update_data: dict[str, Any]
    ) -> None:
        """Handle image update request from plugin."""
        try:
            if update_data["type"] == "raw":
                # Handle raw image data
                image: Image.Image = update_data["image"]
                # Convert to appropriate format and update display
                display_handler = self.display_handlers.get(deck_serial)
                if display_handler:
                    # Create a temporary filter with the image
                    import tempfile

                    from streamdeck_ui.display.image_filter import ImageFilter

                    # Save image to temp file
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                        image.save(f, "PNG")
                        temp_path = f.name

                    # Update filters with the new image
                    from streamdeck_ui.display.filter import Filter

                    filters: list[Filter] = [ImageFilter(temp_path)]
                    display_handler.replace(page, button, filters)

                    # Clean up temp file after a delay (let the filter load it first)
                    import time

                    def cleanup():
                        time.sleep(1)
                        os.unlink(temp_path)

                    threading.Thread(target=cleanup, daemon=True).start()

            elif update_data["type"] == "render":
                # Handle render instructions
                instructions = update_data["instructions"]

                # Update button state with new rendering instructions
                state = self.get_button_state(deck_serial, page, button)
                button_state = self.get_button_state_object(deck_serial, page, button, state)

                if "text" in instructions:
                    button_state.text = instructions["text"]
                if "icon" in instructions:
                    button_state.icon = instructions["icon"]
                if "background_color" in instructions:
                    button_state.background_color = instructions["background_color"]
                if "font_color" in instructions:
                    button_state.font_color = instructions["font_color"]
                if "font_size" in instructions:
                    button_state.font_size = instructions["font_size"]
                if "text_vertical_align" in instructions:
                    button_state.text_vertical_align = instructions["text_vertical_align"]
                if "text_horizontal_align" in instructions:
                    button_state.text_horizontal_align = instructions["text_horizontal_align"]

                # Update filters
                self._update_button_filters(deck_serial, page, button)

        except Exception as e:
            logger.error(f"Error handling plugin image update: {e}", exc_info=True)

    def _handle_plugin_page_switch(self, deck_serial: str, page: int, button: int, duration: int | None) -> None:
        """Handle page switch request from plugin."""
        try:
            if duration:
                # Temporary page switch
                self.set_temp_page(deck_serial, page)

                # Schedule return to previous page
                def restore():
                    time.sleep(duration)
                    self.restore_previous_page(deck_serial)

                threading.Thread(target=restore, daemon=True).start()
            else:
                # Permanent page switch
                self.set_page(deck_serial, page)

        except Exception as e:
            logger.error(f"Error handling plugin page switch: {e}", exc_info=True)

    def _handle_plugin_log_message(self, level: str, message: str) -> None:
        """Handle log message from plugin."""
        log_level_map = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
        }
        logger.log(log_level_map.get(level, logging.INFO), f"Plugin: {message}")

    def notify_button_press(self, serial_number: str, page: int, button: int, pressed: bool) -> None:
        """Notify plugins about button press/release.

        Args:
            serial_number: Stream Deck serial number
            page: Page number
            button: Button number
            pressed: True if pressed, False if released
        """
        assert self.plugin_manager is not None
        instances = self.plugin_manager.get_instances_for_button(serial_number, page, button)
        for instance in instances:
            if pressed:
                instance.send_button_pressed()
            else:
                instance.send_button_released()

    def notify_page_change(self, serial_number: str, old_page: int, new_page: int) -> None:
        """Notify plugins about page changes.

        Args:
            serial_number: Stream Deck serial number
            old_page: Previous page number
            new_page: New page number
        """
        assert self.plugin_manager is not None
        # Notify plugins on old page that they're now hidden
        for instance in self.plugin_manager.instances.values():
            if instance.deck_serial == serial_number and instance.page == old_page:
                instance.send_button_hidden()

        # Notify plugins on new page that they're now visible
        for instance in self.plugin_manager.instances.values():
            if instance.deck_serial == serial_number and instance.page == new_page:
                instance.send_button_visible()
