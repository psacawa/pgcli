"""
User interface Controls for the layout.
"""
from __future__ import unicode_literals

from abc import ABCMeta, abstractmethod
from collections import namedtuple
from six import with_metaclass
from six.moves import range

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.cache import SimpleCache
from prompt_toolkit.filters import to_app_filter
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.search_state import SearchState
from prompt_toolkit.selection import SelectionType
from prompt_toolkit.token import Token
from prompt_toolkit.utils import get_cwidth

from .lexers import Lexer, SimpleLexer
from .processors import Processor, TransformationInput, HighlightSearchProcessor, HighlightSelectionProcessor, DisplayMultipleCursors, merge_processors

from .screen import Point
from .utils import split_lines, token_list_to_text

import six
import time


__all__ = (
    'BufferControl',
    'DummyControl',
    'TokenListControl',
    'UIControl',
    'UIControlKeyBindings',
    'UIContent',
)


class UIControl(with_metaclass(ABCMeta, object)):
    """
    Base class for all user interface controls.
    """
    def reset(self):
        # Default reset. (Doesn't have to be implemented.)
        pass

    def preferred_width(self, app, max_available_width):
        return None

    def preferred_height(self, app, width, max_available_height, wrap_lines):
        return None

    def is_focussable(self, app):
        """
        Tell whether this user control is focussable.
        """
        return False

    @abstractmethod
    def create_content(self, app, width, height):
        """
        Generate the content for this user control.

        Returns a :class:`.UIContent` instance.
        """

    def mouse_handler(self, app, mouse_event):
        """
        Handle mouse events.

        When `NotImplemented` is returned, it means that the given event is not
        handled by the `UIControl` itself. The `Window` or key bindings can
        decide to handle this event as scrolling or changing focus.

        :param app: `Application` instance.
        :param mouse_event: `MouseEvent` instance.
        """
        return NotImplemented

    def move_cursor_down(self, app):
        """
        Request to move the cursor down.
        This happens when scrolling down and the cursor is completely at the
        top.
        """

    def move_cursor_up(self, app):
        """
        Request to move the cursor up.
        """

    def get_key_bindings(self, app):
        """
        The key bindings that are specific for this user control.

        Return a `UIControlKeyBindings` object if some key bindings are
        specified, or `None` otherwise.
        """

    def get_invalidate_events(self):
        """
        Return a list of `Event` objects. This can be a generator.
        (The application collects all these events, in order to bind redraw
        handlers to these events.)
        """
        return []


class UIControlKeyBindings(object):
    """
    Key bindings for a user control.

    :param key_bindings: `KeyBindings` object that contains the key bindings
        which are active when this user control has the focus.
    :param global_key_bindings: `KeyBindings` object that contains the bindings
        which are always active, even when other user controls have the focus.
        (Except if another 'modal' control has the focus.)
    :param modal: If true, mark this user control as modal.
    """
    def __init__(self, key_bindings=None, global_key_bindings=None, modal=False):
        from prompt_toolkit.key_binding.key_bindings import KeyBindingsBase
        assert key_bindings is None or isinstance(key_bindings, KeyBindingsBase)
        assert global_key_bindings is None or isinstance(global_key_bindings, KeyBindingsBase)
        assert isinstance(modal, bool)

        self.key_bindings = key_bindings
        self.global_key_bindings = global_key_bindings
        self.modal = modal


class UIContent(object):
    """
    Content generated by a user control. This content consists of a list of
    lines.

    :param get_line: Callable that takes a line number and returns the current
        line. This is a list of (Token, text) tuples.
    :param line_count: The number of lines.
    :param cursor_position: a :class:`.Point` for the cursor position.
    :param menu_position: a :class:`.Point` for the menu position.
    :param show_cursor: Make the cursor visible.
    """
    def __init__(self, get_line=None, line_count=0,
                 cursor_position=None, menu_position=None, show_cursor=True):
        assert callable(get_line)
        assert isinstance(line_count, six.integer_types)
        assert cursor_position is None or isinstance(cursor_position, Point)
        assert menu_position is None or isinstance(menu_position, Point)

        self.get_line = get_line
        self.line_count = line_count
        self.cursor_position = cursor_position or Point(0, 0)
        self.menu_position = menu_position
        self.show_cursor = show_cursor

        # Cache for line heights. Maps (lineno, width) -> height.
        self._line_heights = {}

    def __getitem__(self, lineno):
        " Make it iterable (iterate line by line). "
        if lineno < self.line_count:
            return self.get_line(lineno)
        else:
            raise IndexError

    def get_height_for_line(self, lineno, width):
        """
        Return the height that a given line would need if it is rendered in a
        space with the given width.
        """
        try:
            return self._line_heights[lineno, width]
        except KeyError:
            text = token_list_to_text(self.get_line(lineno))
            result = self.get_height_for_text(text, width)

            # Cache and return
            self._line_heights[lineno, width] = result
            return result

    @staticmethod
    def get_height_for_text(text, width):
        # Get text width for this line.
        line_width = get_cwidth(text)

        # Calculate height.
        try:
            quotient, remainder = divmod(line_width, width)
        except ZeroDivisionError:
            # Return something very big.
            # (This can happen, when the Window gets very small.)
            return 10 ** 10
        else:
            if remainder:
                quotient += 1  # Like math.ceil.
            return max(1, quotient)


class TokenListControl(UIControl):
    """
    Control that displays a list of (Token, text) tuples.
    (It's mostly optimized for rather small widgets, like toolbars, menus, etc...)

    When this UI control has the focus, the cursor will be shown in the upper
    left corner of this control, unless `get_token` returns a
    ``Token.SetCursorPosition`` token somewhere in the token list, then the
    cursor will be shown there.

    Mouse support:

        The list of tokens can also contain tuples of three items, looking like:
        (Token, text, handler). When mouse support is enabled and the user
        clicks on this token, then the given handler is called. That handler
        should accept two inputs: (Application, MouseEvent) and it should
        either handle the event or return `NotImplemented` in case we want the
        containing Window to handle this event.

    :param focussable: `bool` or `AppFilter`: Tell whether this control is focussable.

    :param get_tokens: Callable that takes an `Application` instance and
        returns the list of (Token, text) tuples to be displayed right now.
    :param key_bindings: a `KeyBindings` object.
    :param global_key_bindings: a `KeyBindings` object that contains always on
        key bindings.
    """
    def __init__(self, get_tokens, focussable=False, key_bindings=None,
                 global_key_bindings=None, modal=False):
        from prompt_toolkit.key_binding.key_bindings import KeyBindingsBase
        assert callable(get_tokens)
        assert key_bindings is None or isinstance(key_bindings, KeyBindingsBase)
        assert global_key_bindings is None or isinstance(global_key_bindings, KeyBindingsBase)
        assert isinstance(modal, bool)

        self.get_tokens = get_tokens
        self.focussable = to_app_filter(focussable)

        # Key bindings.
        self.key_bindings = key_bindings
        self.global_key_bindings = global_key_bindings
        self.modal = modal

        #: Cache for the content.
        self._content_cache = SimpleCache(maxsize=18)
        self._token_cache = SimpleCache(maxsize=1)
            # Only cache one token list. We don't need the previous item.

        # Render info for the mouse support.
        self._tokens = None

    def reset(self):
        self._tokens = None

    def is_focussable(self, app):
        return self.focussable(app)

    def __repr__(self):
        return '%s(%r)' % (self.__class__.__name__, self.get_tokens)

    def _get_tokens_cached(self, app):
        """
        Get tokens, but only retrieve tokens once during one render run.
        (This function is called several times during one rendering, because
        we also need those for calculating the dimensions.)
        """
        return self._token_cache.get(
            app.render_counter, lambda: self.get_tokens(app))

    def preferred_width(self, app, max_available_width):
        """
        Return the preferred width for this control.
        That is the width of the longest line.
        """
        text = token_list_to_text(self._get_tokens_cached(app))
        line_lengths = [get_cwidth(l) for l in text.split('\n')]
        return max(line_lengths)

    def preferred_height(self, app, width, max_available_height, wrap_lines):
        content = self.create_content(app, width, None)
        return content.line_count

    def create_content(self, app, width, height):
        # Get tokens
        tokens_with_mouse_handlers = self._get_tokens_cached(app)
        token_lines_with_mouse_handlers = list(split_lines(tokens_with_mouse_handlers))

        # Strip mouse handlers from tokens.
        token_lines = [
            [tuple(item[:2]) for item in line]
            for line in token_lines_with_mouse_handlers
        ]

        # Keep track of the tokens with mouse handler, for later use in
        # `mouse_handler`.
        self._tokens = tokens_with_mouse_handlers

        # If there is a `Token.SetCursorPosition` in the token list, set the
        # cursor position here.
        def get_cursor_position(token=Token.SetCursorPosition):
            for y, line in enumerate(token_lines):
                x = 0
                for t, text in line:
                    if t == token:
                        return Point(x=x, y=y)
                    x += len(text)
            return None

        # If there is a `Token.SetMenuPosition`, set the menu over here.
        def get_menu_position():
            return get_cursor_position(Token.SetMenuPosition)

        # Create content, or take it from the cache.
        key = (tuple(tokens_with_mouse_handlers), width)

        def get_content():
            return UIContent(get_line=lambda i: token_lines[i],
                             line_count=len(token_lines),
                             cursor_position=get_cursor_position(),
                             menu_position=get_menu_position())

        return self._content_cache.get(key, get_content)

    @classmethod
    def static(cls, tokens):
        def get_static_tokens(app):
            return tokens
        return cls(get_static_tokens)

    def mouse_handler(self, app, mouse_event):
        """
        Handle mouse events.

        (When the token list contained mouse handlers and the user clicked on
        on any of these, the matching handler is called. This handler can still
        return `NotImplemented` in case we want the `Window` to handle this
        particular event.)
        """
        if self._tokens:
            # Read the generator.
            tokens_for_line = list(split_lines(self._tokens))

            try:
                tokens = tokens_for_line[mouse_event.position.y]
            except IndexError:
                return NotImplemented
            else:
                # Find position in the token list.
                xpos = mouse_event.position.x

                # Find mouse handler for this character.
                count = 0
                for item in tokens:
                    count += len(item[1])
                    if count >= xpos:
                        if len(item) >= 3:
                            # Handler found. Call it.
                            # (Handler can return NotImplemented, so return
                            # that result.)
                            handler = item[2]
                            return handler(app, mouse_event)
                        else:
                            break

        # Otherwise, don't handle here.
        return NotImplemented

    def get_key_bindings(self, app):
        return UIControlKeyBindings(
            key_bindings=self.key_bindings,
            global_key_bindings=self.global_key_bindings,
            modal=self.modal)


class DummyControl(UIControl):
    """
    A dummy control object that doesn't paint any content.

    Useful for filling a Window. (The `token` and `char` attributes of the
    `Window` class can be used to define the filling.)
    """
    def create_content(self, app, width, height):
        def get_line(i):
            return []

        return UIContent(
            get_line=get_line,
            line_count=100 ** 100)  # Something very big.

    def is_focussable(self, app):
        return False


_ProcessedLine = namedtuple('_ProcessedLine', 'tokens source_to_display display_to_source')


class BufferControl(UIControl):
    """
    Control for visualising the content of a `Buffer`.

    :param buffer: The `Buffer` object to be displayed.
    :param input_processor: A :class:`~prompt_toolkit.layout.processors.Processor`. (Use
        :func:`~prompt_toolkit.layout.processors.merge_processors` if you want
        to apply multiple processors.)
    :param lexer: :class:`~prompt_toolkit.layout.lexers.Lexer` instance for syntax highlighting.
    :param preview_search: `bool` or `AppFilter`: Show search while typing.
    :param focussable: `bool` or `AppFilter`: Tell whether this control is focussable.
    :param get_search_state: Callable that returns the SearchState to be used.
    :param focus_on_click: Focus this buffer when it's click, but not yet focussed.
    :param key_bindings: a `KeyBindings` object.
    """
    def __init__(self,
                 buffer,
                 input_processor=None,
                 lexer=None,
                 preview_search=False,
                 focussable=True,
                 search_buffer_control=None,
                 get_search_buffer_control=None,
                 get_search_state=None,
                 menu_position=None,
                 focus_on_click=False,
                 key_bindings=None):
        from prompt_toolkit.key_binding.key_bindings import KeyBindingsBase
        assert isinstance(buffer, Buffer)
        assert input_processor is None or isinstance(input_processor, Processor)
        assert menu_position is None or callable(menu_position)
        assert lexer is None or isinstance(lexer, Lexer)
        assert search_buffer_control is None or isinstance(search_buffer_control, BufferControl)
        assert get_search_buffer_control is None or callable(get_search_buffer_control)
        assert not (search_buffer_control and get_search_buffer_control)
        assert get_search_state is None or callable(get_search_state)
        assert key_bindings is None or isinstance(key_bindings, KeyBindingsBase)

        # Default search state.
        if get_search_state is None:
            search_state = SearchState()
            def get_search_state():
                return search_state

        # Default input processor (display search and selection by default.)
        if input_processor is None:
            input_processor = merge_processors([
                HighlightSearchProcessor(),
                HighlightSelectionProcessor(),
                DisplayMultipleCursors(),
            ])

        self.preview_search = to_app_filter(preview_search)
        self.focussable = to_app_filter(focussable)
        self.get_search_state = get_search_state
        self.focus_on_click = to_app_filter(focus_on_click)

        self.input_processor = input_processor
        self.buffer = buffer
        self.menu_position = menu_position
        self.lexer = lexer or SimpleLexer()
        self.get_search_buffer_control = get_search_buffer_control
        self.key_bindings = key_bindings
        self._search_buffer_control = search_buffer_control

        #: Cache for the lexer.
        #: Often, due to cursor movement, undo/redo and window resizing
        #: operations, it happens that a short time, the same document has to be
        #: lexed. This is a faily easy way to cache such an expensive operation.
        self._token_cache = SimpleCache(maxsize=8)

        self._xy_to_cursor_position = None
        self._last_click_timestamp = None
        self._last_get_processed_line = None

    @property
    def search_buffer_control(self):
        if self.get_search_buffer_control is not None:
            return self.get_search_buffer_control()
        else:
            return self._search_buffer_control

    @property
    def search_buffer(self):
        control = self.search_buffer_control
        if control is not None:
            return control.buffer

    @property
    def search_state(self):
        return self.get_search_state()

    def is_focussable(self, app):
        return self.focussable(app)

    def preferred_width(self, app, max_available_width):
        """
        This should return the preferred width.

        Note: We don't specify a preferred width according to the content,
              because it would be too expensive. Calculating the preferred
              width can be done by calculating the longest line, but this would
              require applying all the processors to each line. This is
              unfeasible for a larger document, and doing it for small
              documents only would result in inconsistent behaviour.
        """
        return None

    def preferred_height(self, app, width, max_available_height, wrap_lines):
        # Calculate the content height, if it was drawn on a screen with the
        # given width.
        height = 0
        content = self.create_content(app, width, None)

        # When line wrapping is off, the height should be equal to the amount
        # of lines.
        if not wrap_lines:
            return content.line_count

        # When the number of lines exceeds the max_available_height, just
        # return max_available_height. No need to calculate anything.
        if content.line_count >= max_available_height:
            return max_available_height

        for i in range(content.line_count):
            height += content.get_height_for_line(i, width)

            if height >= max_available_height:
                return max_available_height

        return height

    def _get_tokens_for_line_func(self, app, document):
        """
        Create a function that returns the tokens for a given line.
        """
        # Cache using `document.text`.
        def get_tokens_for_line():
            return self.lexer.lex_document(app, document)

        return self._token_cache.get(document.text, get_tokens_for_line)

    def _create_get_processed_line_func(self, app, document, width, height):
        """
        Create a function that takes a line number of the current document and
        returns a _ProcessedLine(processed_tokens, source_to_display, display_to_source)
        tuple.
        """
        merged_processor = self.input_processor

        def transform(lineno, tokens):
            " Transform the tokens for a given line number. "
            # Get cursor position at this line.
            if document.cursor_position_row == lineno:
                cursor_column = document.cursor_position_col
            else:
                cursor_column = None

            def source_to_display(i):
                """ X position from the buffer to the x position in the
                processed token list. By default, we start from the 'identity'
                operation. """
                return i

            transformation = merged_processor.apply_transformation(
                TransformationInput(
                    app, self, document, lineno, source_to_display, tokens,
                    width, height))

            if cursor_column:
                cursor_column = transformation.source_to_display(cursor_column)

            return _ProcessedLine(
                transformation.tokens,
                transformation.source_to_display,
                transformation.display_to_source)

        def create_func():
            get_line = self._get_tokens_for_line_func(app, document)
            cache = {}

            def get_processed_line(i):
                try:
                    return cache[i]
                except KeyError:
                    processed_line = transform(i, get_line(i))
                    cache[i] = processed_line
                    return processed_line
            return get_processed_line

        return create_func()

    def create_content(self, app, width, height):
        """
        Create a UIContent.
        """
        buffer = self.buffer

        # Get the document to be shown. If we are currently searching (the
        # search buffer has focus, and the preview_search filter is enabled),
        # then use the search document, which has possibly a different
        # text/cursor position.)
        search_control = self.search_buffer_control
        preview_now = bool(
            search_control and search_control.buffer.text and self.preview_search(app))

        if preview_now:
            ss = self.search_state

            document = buffer.document_for_search(SearchState(
                text=search_control.buffer.text,
                direction=ss.direction,
                ignore_case=ss.ignore_case))
        else:
            document = buffer.document

        get_processed_line = self._create_get_processed_line_func(
            app, document, width, height)
        self._last_get_processed_line = get_processed_line

        def translate_rowcol(row, col):
            " Return the content column for this coordinate. "
            return Point(y=row, x=get_processed_line(row).source_to_display(col))

        def get_line(i):
            " Return the tokens for a given line number. "
            tokens = get_processed_line(i).tokens

            # Add a space at the end, because that is a possible cursor
            # position. (When inserting after the input.) We should do this on
            # all the lines, not just the line containing the cursor. (Because
            # otherwise, line wrapping/scrolling could change when moving the
            # cursor around.)
            tokens = tokens + [(Token, ' ')]
            return tokens

        content = UIContent(
            get_line=get_line,
            line_count=document.line_count,
            cursor_position=translate_rowcol(document.cursor_position_row,
                                             document.cursor_position_col))

        # If there is an auto completion going on, use that start point for a
        # pop-up menu position. (But only when this buffer has the focus --
        # there is only one place for a menu, determined by the focussed buffer.)
        if app.layout.current_control == self:
            menu_position = self.menu_position(app) if self.menu_position else None
            if menu_position is not None:
                assert isinstance(menu_position, int)
                menu_row, menu_col = buffer.document.translate_index_to_position(menu_position)
                content.menu_position = translate_rowcol(menu_row, menu_col)
            elif buffer.complete_state:
                # Position for completion menu.
                # Note: We use 'min', because the original cursor position could be
                #       behind the input string when the actual completion is for
                #       some reason shorter than the text we had before. (A completion
                #       can change and shorten the input.)
                menu_row, menu_col = buffer.document.translate_index_to_position(
                    min(buffer.cursor_position,
                        buffer.complete_state.original_document.cursor_position))
                content.menu_position = translate_rowcol(menu_row, menu_col)
            else:
                content.menu_position = None

        return content

    def mouse_handler(self, app, mouse_event):
        """
        Mouse handler for this control.
        """
        buffer = self.buffer
        position = mouse_event.position

        # Focus buffer when clicked.
        if app.layout.current_control == self:
            if self._last_get_processed_line:
                processed_line = self._last_get_processed_line(position.y)

                # Translate coordinates back to the cursor position of the
                # original input.
                xpos = processed_line.display_to_source(position.x)
                index = buffer.document.translate_row_col_to_index(position.y, xpos)

                # Set the cursor position.
                if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
                    buffer.exit_selection()
                    buffer.cursor_position = index

                elif mouse_event.event_type == MouseEventType.MOUSE_UP:
                    # When the cursor was moved to another place, select the text.
                    # (The >1 is actually a small but acceptable workaround for
                    # selecting text in Vi navigation mode. In navigation mode,
                    # the cursor can never be after the text, so the cursor
                    # will be repositioned automatically.)
                    if abs(buffer.cursor_position - index) > 1:
                        buffer.start_selection(selection_type=SelectionType.CHARACTERS)
                        buffer.cursor_position = index

                    # Select word around cursor on double click.
                    # Two MOUSE_UP events in a short timespan are considered a double click.
                    double_click = self._last_click_timestamp and time.time() - self._last_click_timestamp < .3
                    self._last_click_timestamp = time.time()

                    if double_click:
                        start, end = buffer.document.find_boundaries_of_current_word()
                        buffer.cursor_position += start
                        buffer.start_selection(selection_type=SelectionType.CHARACTERS)
                        buffer.cursor_position += end - start
                else:
                    # Don't handle scroll events here.
                    return NotImplemented

        # Not focussed, but focussing on click events.
        else:
            if self.focus_on_click(app) and mouse_event.event_type == MouseEventType.MOUSE_UP:
                # Focus happens on mouseup. (If we did this on mousedown, the
                # up event will be received at the point where this widget is
                # focussed and be handled anyway.)
                app.layout.current_control = self
            else:
                return NotImplemented

    def move_cursor_down(self, app):
        b = self.buffer
        b.cursor_position += b.document.get_cursor_down_position()

    def move_cursor_up(self, app):
        b = self.buffer
        b.cursor_position += b.document.get_cursor_up_position()

    def get_key_bindings(self, app):
        """
        When additional key bindings are given. Return these.
        """
        return UIControlKeyBindings(key_bindings=self.key_bindings)

    def get_invalidate_events(self):
        """
        Return the Window invalidate events.
        """
        # Whenever the buffer changes, the UI has to be updated.
        yield self.buffer.on_text_changed
        yield self.buffer.on_cursor_position_changed

        yield self.buffer.on_completions_changed
        yield self.buffer.on_suggestion_set
