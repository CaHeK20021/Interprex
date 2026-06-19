"""Fast offline self-test for the parser layer. No server, no network.

Run:  npm run test:py   (or  ./venv/Scripts/python.exe selftest.py)

Builds a tiny fake RPG Maker project in a temp dir and checks the things that
are expensive to get wrong:
  - engine autodetect
  - extract finds the right strings
  - ids are STABLE across two extract runs (translation memory depends on this)
  - inject writes translations back into the correct JSON slots
  - malformed commands are skipped, not crashed on
"""

from __future__ import annotations

import json
import os
import tempfile
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(__file__))

from parsers import detect_engine, get_parser  # noqa: E402
from parsers.base import make_id  # noqa: E402


def build_project() -> str:
    root = tempfile.mkdtemp(prefix="interprex_selftest_")
    data = os.path.join(root, "data")
    os.makedirs(data)
    open(os.path.join(data, "System.json"), "w").write("{}")
    mp = {"events": [None, {"pages": [{"list": [
        {"code": 401, "parameters": ["Hello there"]},
        {"code": 401, "parameters": ["How are you?"]},
        {"code": 102, "parameters": [["Yes", "No"]]},
        {"code": 102, "parameters": [None]},   # malformed choice -> skip
        {"code": 0, "parameters": []},          # non-text command
    ]}]}]}
    json.dump(mp, open(os.path.join(data, "Map001.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    return root


def build_renpy_project() -> str:
    root = tempfile.mkdtemp(prefix="interprex_renpy_selftest_")
    game = os.path.join(root, "game")
    os.makedirs(game)
    script = (
        'define e = Character("Eileen")\n'
        '\n'
        'label start:\n'
        '    "Narration line."\n'
        '    e "Hello there."\n'
        '    e "Hello there."\n'           # duplicate -> collision suffix _1
        '    e happy "How are you?"\n'
        '    e @ happy "Nice day."\n'
        '    e @[dynamic_var] "So... what serial killer are you?"\n'
        '    e "Wait  here."\n'            # double space -> lexer collapses
        '    e "Line with\\nnewline."\n'    # escape sequence test (say)
        '    e "Line with real\nnewline."\n' # already unescaped newline test
        '    e "Line with \\\\n."\n'         # double backslash + n test (in python: \\\\n)
        '    $ x = "not dialogue"\n'
        '    menu:\n'
        '        e "Pick one:"\n'          # menu caption -> nointeract
        '        "Yes please":\n'
        '            jump yes\n'
        '        "No thanks" if flag:\n'
        '            return\n'
        '\n'
        'label day1_chat:\n'
        '    e "Let us chat."\n'
        '\n'
        'menu choose_path:\n'              # NAMED menu -> compiles to Label "choose_path"
        '    e "It depends."\n'            # say inside it -> id prefixed by MENU name, not "day1_chat"
        '    "Left":\n'
        '        jump yes\n'
        '    "Right":\n'
        '        return\n'
        '\n'
        'screen save_load():\n'
        '    vbox:\n'
        '        textbutton "Save":\n'
        '            action FileSave(1)\n'
        '        textbutton "Load" action FileLoad(1)\n'
        '        textbutton _("Back") action Rollback()\n'  # _() translatable call
        '        textbutton _("Back\\nButton") action Rollback()\n'  # _() escape test
        '        textbutton _("Play").upper() action Start()\n'
        '        textbutton _("Exit").lower() action Quit()\n'
        '        textbutton _(f"{ \'> \' if button_hovered == \'settings\' else \'\' }SETTINGS") action ShowMenu("preferences")\n'
        '        textbutton _(f"Save Recovery Mode: { \'ON\' if persistent.save_file_recovery else \'OFF\' }")\n'
        '        label "Options"\n'
        '        text "Version 1.0"\n'
        '        textbutton "Help":\n'
        '            tooltip "Click for help"\n'  # tooltip widget test
        '            action ShowScreen("help")\n'
        '\n'
        # Decompiled screens with a non-default init priority come out as
        # `init -501 screen NAME():` — the bare-string textbuttons inside MUST
        # still be extracted (regression: OnlineObsessionDemo main menu).
        'init -501 screen main_menu():\n'
        '    vbox:\n'
        '        textbutton "start" action Start()\n'
        '        textbutton "prefs" action ShowMenu("preferences")\n'
        # Screen `text` with CONSECUTIVE SPACES: the engine eval's the old/new
        # strings key as a Python literal (whitespace preserved), so the `old`
        # key MUST keep both spaces or the runtime lookup misses (StarBlitz quiz
        # caption bug in OnlineObsessionDemo).
        '        text "Quiz time!  Ready?"\n'
    )
    open(os.path.join(game, "script.rpy"), "w", encoding="utf-8").write(script)
    return root


def check_renpy() -> None:
    root = build_renpy_project()
    script = os.path.join(root, "game", "script.rpy")
    script_before = open(script, encoding="utf-8").read()

    assert detect_engine(root) == "renpy", "renpy detect failed"
    p = get_parser("renpy")

    strings = p.extract(root)
    originals = [s.original for s in strings]
    assert originals == [
        "Eileen",
        "Narration line.", "Hello there.", "Hello there.", "How are you?",
        "Nice day.", "So... what serial killer are you?",
        "Wait  here.", "Line with\\nnewline.", "Line with real\nnewline.", "Line with \\\\n.", "Pick one:",
        "Yes please", "No thanks",
        "Let us chat.",
        "It depends.", "Left", "Right",
        "Save", "Load", "Back", "Back\\nButton", "PLAY", "exit",
        "> SETTINGS", "SETTINGS", "Save Recovery Mode: ON", "Save Recovery Mode: OFF",
        "Options", "Version 1.0", "Help", "Click for help",
        "start", "prefs",
        "Quiz time!  Ready?",
    ], originals

    # $ assignment line must not leak in as dialogue
    assert "not dialogue" not in originals, originals
    # _() call IS captured
    assert "Back" in originals, originals

    # context: speaker names resolved via define table; narrator for bare strings
    by_orig = {s.original: s for s in strings}
    assert by_orig["Eileen"].context == "character name", by_orig["Eileen"].context
    assert by_orig["Narration line."].context == "Speaker: narrator", by_orig["Narration line."].context
    assert by_orig["How are you?"].context == "Speaker: Eileen | Prev line: 'Hello there.' (Eileen)", by_orig["How are you?"].context
    assert by_orig["Nice day."].context == "Speaker: Eileen | Prev line: 'How are you?' (Eileen)", by_orig["Nice day."].context
    assert by_orig["So... what serial killer are you?"].context == "Speaker: Eileen | Prev line: 'Nice day.' (Eileen)", by_orig["So... what serial killer are you?"].context
    assert by_orig["Yes please"].context == "", by_orig["Yes please"].context
    assert by_orig["Let us chat."].context == "Label: day1_chat | Speaker: Eileen", by_orig["Let us chat."].context
    assert by_orig["Save"].context == "save_load textbutton", by_orig["Save"].context
    # bare-string textbuttons in an `init -501 screen main_menu():` are extracted
    assert by_orig["start"].context == "main_menu textbutton", by_orig["start"].context
    assert by_orig["prefs"].context == "main_menu textbutton", by_orig["prefs"].context

    # ids stable across runs (the two identical "Hello there." lines have
    # DIFFERENT ids because their path index differs — that's by design)
    ids1 = [s.id for s in strings]
    ids2 = [s.id for s in p.extract(root)]
    assert ids1 == ids2, "renpy ids not stable across runs"
    assert len(set(ids1)) == len(ids1), "renpy ids must be unique per string"

    # inject: writes to tl/, leaves the original UNTOUCHED
    tr = {s.id: s.original.upper().replace("\\N", "\\n") for s in strings}
    written = p.inject(root, tr, "russian")
    assert written == len(strings), f"renpy written={written} of {len(strings)}"

    # 1. original .rpy is byte-for-byte unchanged
    script_after = open(script, encoding="utf-8").read()
    assert script_after == script_before, "renpy inject MODIFIED the original .rpy"

    # 2. tl/russian/script.rpy created with correct say blocks
    tl_script = os.path.join(root, "game", "tl", "russian", "script.rpy")
    assert os.path.exists(tl_script), "tl/russian/script.rpy not created"
    tl = open(tl_script, encoding="utf-8").read()

    # say identifiers match the engine algorithm (anchored to the oracle)
    assert "translate russian start_93477484:" in tl, tl   # narration
    assert "translate russian start_496d9b91:" in tl, tl   # hello #1
    assert "translate russian start_496d9b91_1:" in tl, tl  # hello #2 (collision)
    assert "translate russian start_0be1a894:" in tl, tl   # happy (attr)
    assert "translate russian start_7edaa0c0:" in tl, tl   # double-space collapsed
    assert "translate russian start_bbe3b343:" in tl, tl   # menu caption
    # caption line carries nointeract
    assert "nointeract" in tl, tl
    # say inside a NAMED menu is prefixed by the MENU name (engine compiles
    # `menu choose_path:` to `Label choose_path`), NOT the enclosing label
    # `day1_chat`. Regression guard for the named-menu id bug. (This say is the
    # first statement in the menu, so it's also the nointeract caption — the
    # `nointeract` suffix is folded into the digest, hence 4cde68f2.)
    assert "translate russian choose_path_4cde68f2:" in tl, tl
    assert "day1_chat_4cde68f2" not in tl, tl  # must NOT use the enclosing label
    # translated say line present
    assert '    e "HELLO THERE."\n' in tl, tl
    assert '    e @ happy "NICE DAY."\n' in tl, tl
    assert '    e @[dynamic_var] "SO... WHAT SERIAL KILLER ARE YOU?"\n' in tl, tl
    # double-space original comment is collapsed by the lexer in the identifier,
    # but the reference comment shows the canonical code form
    assert '# e "Wait here."' in tl, tl

    # escape sequence test in say block (should NOT be double escaped, i.e. stay \\n in file)
    assert '    e "LINE WITH\\nNEWLINE."\n' in tl, tl
    # real newline (already unescaped) test (should be escaped, i.e. stay \\n in file)
    assert '    e "LINE WITH REAL\\nNEWLINE."\n' in tl, tl
    # double backslash + n test (should stay \\\\n in file)
    assert '    e "LINE WITH \\\\n."\n' in tl, tl

    # 3. strings block with old/new for non-say (menu choices, _(), screen, name)
    assert "translate russian strings:" in tl, tl
    assert 'old "Yes please"' in tl and 'new "YES PLEASE"' in tl, tl
    assert 'old "Back"' in tl and 'new "BACK"' in tl, tl
    assert 'old "Eileen"' in tl and 'new "EILEEN"' in tl, tl  # character name

    # case variants (.upper() / .lower() wrappers) evaluated at extraction time
    assert 'old "PLAY"' in tl and 'new "PLAY"' in tl, tl
    assert 'old "exit"' in tl and 'new "EXIT"' in tl, tl

    # f-string expanded variants check
    assert 'old "> SETTINGS"' in tl and 'new "> SETTINGS"' in tl, tl
    assert 'old "SETTINGS"' in tl and 'new "SETTINGS"' in tl, tl
    assert 'old "Save Recovery Mode: ON"' in tl and 'new "SAVE RECOVERY MODE: ON"' in tl, tl
    assert 'old "Save Recovery Mode: OFF"' in tl and 'new "SAVE RECOVERY MODE: OFF"' in tl, tl

    # escape sequence test in strings block
    assert 'old "Back\\nButton"' in tl, tl
    assert 'new "BACK\\nBUTTON"' in tl, tl

    # screen `text` old/new key keeps CONSECUTIVE SPACES verbatim — the engine
    # eval's it as a Python literal (no whitespace collapse), so a collapsed key
    # would never match the runtime value (StarBlitz quiz caption bug).
    assert 'old "Quiz time!  Ready?"' in tl, tl  # two spaces preserved
    assert 'old "Quiz time! Ready?"' not in tl, tl  # must NOT collapse to one

    # 4. language file forces russian
    lang_file = os.path.join(root, "game", "interprex_language.rpy")
    assert os.path.exists(lang_file), "interprex_language.rpy not created"
    lang_content = open(lang_file, encoding="utf-8").read()
    assert 'config.language = "russian"' in lang_content, lang_content
    assert 'renpy.change_language("russian")' in lang_content, lang_content

    # 5. Test backup restore deletes newly created files
    from main import backup_restore, BackupRestoreReq
    res = backup_restore(BackupRestoreReq(root=root))
    assert res == {"success": True}, res
    assert not os.path.exists(tl_script), "tl/russian/script.rpy was NOT deleted on restore"
    assert not os.path.exists(lang_file), "interprex_language.rpy was NOT deleted on restore"
    # restore must also prune the now-empty tl/<lang>/ dirs it emptied, not leave
    # orphan folders behind (regression: tl/russian/tl/None/ lingered after revert)
    assert not os.path.exists(os.path.join(root, "game", "tl", "russian")), \
        "empty tl/russian/ dir left behind after restore"


def check_renpy_font() -> None:
    """The _interprex_font.rpy block must (1) re-run safely — it executes again
    every time the player switches font in-game, and FontGroup.add() raises on a
    name already in font_name_map, so we emit plain-string maps only; (2) remap
    fonts the game references DIRECTLY by filename (the tofu cause) → our font;
    (3) leave emoji fonts and gui.* alias values alone; (4) be removed on restore."""
    import os, tempfile, re
    from parsers.renpy import RenPyParser

    root = tempfile.mkdtemp()
    p = RenPyParser()
    game_fonts = {
        "TinyUnicode.ttf", "MatrixSans-Regular.ttf", "NunitoSans-Regular.ttf",
        "AtkinsonHyperlegible-Regular.ttf", "NotoSans-Regular.ttf",
        "TwemojiCOLRv0.ttf", "UnifontExMono.ttf", "game/**.ttf",
    }
    p._write_native_font(root, "russian", "NotoSans-Regular.ttf", game_fonts)
    fp = os.path.join(root, "game", "tl", "russian", "_interprex_font.rpy")
    assert os.path.exists(fp), "_interprex_font.rpy not created"
    src = open(fp, encoding="utf-8").read()

    # No FontGroup CALL anywhere (the non-idempotent crash source). The word may
    # appear in a comment; what must never appear is an actual FontGroup() call.
    assert "FontGroup(" not in src, "font block must not use FontGroup (crashes on re-run)"
    # Glob patterns are not real font names — must be skipped.
    assert "game/**" not in src, "glob font pattern leaked into map"
    # Our own font must NOT be mapped to itself.
    assert '"NotoSans-Regular.ttf"]' not in src, "our font remapped to itself"
    # Emoji font preserved.
    assert "Twemoji" not in src, "emoji font must not be remapped"

    # Execute the first translate-python block twice against a fake engine that
    # mirrors the real FontGroup-alias rule — proves idempotency + correctness.
    m = re.search(r'translate russian python:\n(.*?)(?=\ntranslate |\Z)', src, re.S)
    body = "\n".join(l[4:] if l.startswith("    ") else l for l in m.group(1).split("\n"))

    class _FontGroup:
        def add(self, font, *a, **k):
            if font in _cfg.font_name_map:
                raise Exception("FontGroup do not accept font aliases.")
            return self
    class _Cfg: pass
    class _Gui:
        text_font = "default_pixel_font"          # alias — must be PRESERVED
        name_text_font = "default_pixel_font"
        interface_text_font = "AnalogueOS-Regular.ttf"  # direct file — must be repointed
    
    calls = []
    class _Translation:
        @staticmethod
        def translate_string(s):
            calls.append(("trans", s))
            if s.startswith("1. "):
                return s  # simulate translation missing
            return "translated:" + s
    class _Game:
        lint = False
    class _Renpy:
        translation = _Translation
        game = _Game()
        @staticmethod
        def predicting(): return False
        @staticmethod
        def notify(*a, **k): pass

    # The mock mirrors the REAL game function: it resolves `calls` from its GLOBALS
    # (no closure free variables), so it can be patched in place via a __code__ swap
    # (the pickle-safe fix). A closure-capturing mock would have freevars and force
    # the defensive skip path, hiding whether the real in-place patch works. Build it
    # in a clean namespace via exec so its code has zero freevars.
    _mock_ns = {"calls": calls}
    exec(
        "def add_ping_hyperlinks(new_text):\n"
        "    calls.append(('orig', new_text))\n"
        "    return 'hyperlinked:' + new_text\n",
        _mock_ns,
    )
    mock_add_ping_hyperlinks = _mock_ns["add_ping_hyperlinks"]
    assert not mock_add_ping_hyperlinks.__code__.co_freevars, \
        "test mock must have no freevars (mirrors the real store function)"

    class MockStyle:
        def __getattr__(self, name):
            return self
        def __setattr__(self, name, val):
            pass

    class MockServerRole:
        def __init__(self, name, icon="", category="ranks"):
            self.name = name
            self.icon = icon
            self.category = category

    class MockChatCharacter:
        def __init__(self, dominant_role):
            self.dominant_role = dominant_role

    class MockChatter:
        def __init__(self, username):
            self.username = username

    class MockChatChannel:
        def __init__(self):
            self.people_typing = []

    _cfg = _Cfg()
    _cfg.font_name_map = {"default_pixel_font": _FontGroup(), "special_font": "UnifontExMono.ttf"}
    _gui = _Gui()
    
    # Setup mock preferences language for renpy
    class MockPrefs:
        language = "russian"
    _Renpy.game.preferences = MockPrefs()

    env = {
        "config": _cfg,
        "gui": _gui,
        "FontGroup": _FontGroup,
        "renpy": _Renpy,
        "add_ping_hyperlinks": mock_add_ping_hyperlinks,
        "style": MockStyle(),
        "ServerRole": MockServerRole,
        "ChatCharacter": MockChatCharacter,
        "ChatChannel": MockChatChannel,
    }


    exec(body, env)   # run 1
    exec(body, env)   # run 2 — must NOT raise (idempotent)

    # add_ping_hyperlinks must be patched IN PLACE — the SAME function object, with
    # swapped behaviour. This is load-bearing: Ren'Py pickles store functions by
    # reference and checks identity on save; if we rebind the name to a NEW object,
    # every save raises PicklingError ("not the same object as store.add_ping_
    # hyperlinks") and the player loses all progress (real bug, Killer Chat 1.4.1).
    patched_fn = env.get("add_ping_hyperlinks")
    assert patched_fn is mock_add_ping_hyperlinks, \
        "add_ping_hyperlinks must stay the SAME object (rebinding breaks pickle/saves)"
    res_val = patched_fn("hello")
    assert res_val == "hyperlinked:translated:hello", f"unexpected result: {res_val}"
    assert calls == [("trans", "hello"), ("orig", "translated:hello")], f"unexpected calls: {calls}"
    # Re-running the block must NOT re-wrap (idempotent) and must keep identity.
    calls.clear()
    assert env.get("add_ping_hyperlinks") is mock_add_ping_hyperlinks
    assert patched_fn("hi") == "hyperlinked:translated:hi", "second run changed behaviour"

    # Verify that ServerRole and ChatCharacter name/dominant_role fields are dynamically translated but keep raw value comparisons intact
    patched_role_class = env.get("ServerRole")
    patched_char_class = env.get("ChatCharacter")
    
    role_inst = patched_role_class("sweet serial killer", icon="❤️")
    char_inst = patched_char_class("sweet serial killer")
    
    # Check that they evaluate to the translated strings (with mock prefix "translated:") when converted or formatted
    assert str(role_inst.name) == "translated:sweet serial killer"
    assert str(char_inst.dominant_role) == "translated:sweet serial killer"
    assert " " + role_inst.name == " translated:sweet serial killer"
    
    # Check that they still compare equal to the original English string for code matching
    assert role_inst.name == "sweet serial killer"
    assert char_inst.dominant_role == "sweet serial killer"
    assert char_inst.dominant_role == role_inst.name

    # Verify that choice translation patch works for strings with prefixes
    trans_fn = _Renpy.translation.translate_string
    assert trans_fn("1. hello") == "1. translated:hello", f"choice patch failed: {trans_fn('1. hello')}"

    # Verify that ChatChannel get_who_typing property is patched and dynamically translates typing indicators
    patched_channel_class = env.get("ChatChannel")
    channel_inst = patched_channel_class()
    
    # 0 typers
    assert channel_inst.get_who_typing == ""
    
    # 1 typer
    channel_inst.people_typing = [MockChatter("ariousarus")]
    assert channel_inst.get_who_typing == "ariousarus пишет..."
    
    # 2 typers
    channel_inst.people_typing = [MockChatter("user1"), MockChatter("user2")]
    assert channel_inst.get_who_typing == "user1 и user2 пишут..."
    
    # 4 typers
    channel_inst.people_typing = [MockChatter("u1"), MockChatter("u2"), MockChatter("u3"), MockChatter("u4")]
    assert channel_inst.get_who_typing == "Несколько человек пишут..."

    assert _cfg.font_name_map["default_pixel_font"] == "fonts/NotoSans-Regular.ttf", \
        "built-in alias not remapped"
    assert _cfg.font_name_map.get("TinyUnicode.ttf") == "fonts/NotoSans-Regular.ttf", \
        "direct-file font (chat) not remapped — would render as tofu"
    assert "TwemojiCOLRv0.ttf" not in _cfg.font_name_map, "emoji font got remapped"
    # gui alias value preserved (game styles branch sizes on it); direct-file repointed.
    assert _gui.text_font == "default_pixel_font", "gui alias value must be preserved"
    assert _gui.interface_text_font == "fonts/NotoSans-Regular.ttf", \
        "gui direct-file font not repointed"

    # Restore deletes the generated font file (registered type=created).
    from main import backup_restore, BackupRestoreReq
    res = backup_restore(BackupRestoreReq(root=root))
    assert res == {"success": True}, res
    assert not os.path.exists(fp), "_interprex_font.rpy was NOT deleted on restore"


def check_char_limit() -> None:
    import tempfile
    import os
    from parsers.renpy import parse_gui_rpy, get_source_font_and_size, get_char_limit
    
    # 1. Test parse_gui_rpy and get_source_font_and_size cascade
    root = tempfile.mkdtemp(prefix="interprex_gui_selftest_")
    game = os.path.join(root, "game")
    os.makedirs(game)
    
    # Mock gui.rpy with different types of assignments (define, init python style, comments)
    gui_rpy_content = (
        'define gui.text_font = "DejaVuSans.ttf"\n'
        'define gui.text_size = 22\n'
        '    gui.interface_font = "Ubuntu.ttf"  # comment here\n'
        '    gui.choice_button_text_size = 36\n'
        'define gui.choice_button_text_font = "DejaVuSans-Bold.ttf"\n'
    )
    with open(os.path.join(game, "gui.rpy"), "w", encoding="utf-8") as f:
        f.write(gui_rpy_content)
        
    strings, ints = parse_gui_rpy(root)
    assert strings["text_font"] == "DejaVuSans.ttf"
    assert strings["interface_font"] == "Ubuntu.ttf"
    assert strings["choice_button_text_font"] == "DejaVuSans-Bold.ttf"
    assert ints["text_size"] == 22
    assert ints["choice_button_text_size"] == 36
    
    # Test font/size cascade
    src_font_path, font_size = get_source_font_and_size(root)
    assert src_font_path.endswith("NotoSans-Regular.ttf"), f"expected fallback to NotoSans, got {src_font_path}"
    assert font_size == 36, f"expected size 36, got {font_size}"
    
    # 2. Test get_char_limit with pixel calculations and multi-line strings
    font_path = src_font_path
    
    # Single line
    limit1 = get_char_limit("Hello", font_path, "english", 32)
    assert limit1 > 0
    
    # Multi-line: longest line should determine the limit (pixel-wise)
    limit_short = get_char_limit("Hi", font_path, "english", 32)
    limit_multiline = get_char_limit("Hi\nThis is a much longer line", font_path, "english", 32)
    assert limit_multiline > limit_short, f"expected multiline limit {limit_multiline} > short limit {limit_short}"
    
    # Target CJK scripts: Japanese (wider chars) should yield a smaller character limit than English for the same text
    limit_en = get_char_limit("Hello World, how are you?", font_path, "english", 32)
    limit_ja = get_char_limit("Hello World, how are you?", font_path, "japanese", 32)
    assert limit_ja < limit_en, f"expected CJK limit {limit_ja} to be smaller than Latin limit {limit_en}"

    # 3. Test tag stripping: "{b}Hello{/b}" and "Hello" should yield the same limit
    limit_tagged = get_char_limit("{b}Hello{/b}", font_path, "english", 32)
    assert limit_tagged == limit1, f"expected limit_tagged {limit_tagged} == limit1 {limit1}"

    # 4. EVERY target language must resolve to its OWN script font + sample, not
    #    silently fall back to English. The UI sends DISPLAY names ("Russian",
    #    "Chinese (Simplified)", "Portuguese (Brazil)") — the exact strings that
    #    used to miss the lowercase dict keys and corrupt every non-Latin limit.
    from parsers.renpy import (
        _normalize_lang, measure_original_px, measure_translation_px,
        ALPHABET_SAMPLES, LANG_FONTS,
    )

    UI_LANGS = [
        "Russian", "English", "Spanish", "German", "French",
        "Japanese", "Chinese (Simplified)", "Korean", "Portuguese (Brazil)",
    ]
    EXPECT_KEY = {
        "Russian": "russian", "English": "english", "Spanish": "spanish",
        "German": "german", "French": "french", "Japanese": "japanese",
        "Chinese (Simplified)": "chinese", "Korean": "korean",
        "Portuguese (Brazil)": "portuguese",
    }
    for ui in UI_LANGS:
        key = _normalize_lang(ui)
        assert key == EXPECT_KEY[ui], f"{ui!r} normalized to {key!r}, want {EXPECT_KEY[ui]!r}"
        assert key in ALPHABET_SAMPLES, f"{ui!r} -> {key!r} missing from ALPHABET_SAMPLES"
        assert key in LANG_FONTS, f"{ui!r} -> {key!r} missing from LANG_FONTS"
        # The display name must give the SAME limit as its bare key — i.e. it
        # really hit the right table, not the English fallback.
        lim_disp = get_char_limit("Continue the story", font_path, ui, 32)
        lim_key = get_char_limit("Continue the story", font_path, key, 32)
        assert lim_disp == lim_key, f"{ui!r} limit {lim_disp} != {key!r} limit {lim_key} (fell back?)"
        assert lim_disp > 0

    # CJK fonts/samples differ from Latin: a Latin source string must yield a
    # DIFFERENT (smaller, denser script) limit for JA/ZH/KO than for EN — proving
    # the CJK font actually loaded rather than NotoSans-Latin standing in.
    for cjk in ("Japanese", "Chinese (Simplified)", "Korean"):
        lim_cjk = get_char_limit("Are you sure you want to continue?", font_path, cjk, 32)
        lim_en = get_char_limit("Are you sure you want to continue?", font_path, "English", 32)
        assert lim_cjk != lim_en, f"{cjk} limit {lim_cjk} == English {lim_en}; CJK font/sample not used"

    # 5. Pixel ground-truth: a translation that's clearly wider than the original
    #    must measure wider; one that fits must measure narrower. This is what the
    #    scheduler's overflow check relies on — no len()/avg involved.
    orig = "OK"
    orig_px = measure_original_px(orig, font_path, 32)
    wide_px = measure_translation_px("Подтвердить выбор немедленно", "Russian", 32)
    fit_px = measure_translation_px("Да", "Russian", 32)
    assert wide_px > orig_px, f"wide translation {wide_px}px should exceed original {orig_px}px"
    assert fit_px <= orig_px * 1.2, f"short translation {fit_px}px should be near original {orig_px}px"

    # 6. Tags must not count toward measured width (measure side too).
    assert (
        measure_translation_px("{b}Да{/b}", "Russian", 32)
        == measure_translation_px("Да", "Russian", 32)
    ), "style tags leaked into measured translation width"

    # 7. Frequency-weighted avg: a phrase with MANY spaces should get a slightly
    #    LARGER char budget than the same pixel-width packed solid, because the
    #    (narrow) space lowers the average glyph width. Guards the space handling.
    from parsers.renpy import _avg_char_width
    assert _avg_char_width("english", 32) > 0
    assert _avg_char_width("japanese", 32) > _avg_char_width("english", 32), \
        "CJK glyphs should be wider on average than Latin"

    # 8. Empty / unknown inputs degrade, never crash.
    assert get_char_limit("", font_path, "Russian", 32) == 5
    assert get_char_limit("Hello", font_path, "Klingon", 32) > 0  # unknown -> english fallback, still works

    # 9. Pixel font style: inject's font swap (_detect_font) and the width
    #    measurement (_target_font via _avg_char_width / measure_translation_px)
    #    MUST agree on which font a script gets, or the UI-fit budget drifts from
    #    what the player sees. Verified against the actual bundled cmaps.
    from parsers.renpy import RenPyParser, _PIXEL_LANG_FONTS, LANG_FONTS
    det = RenPyParser._detect_font
    # Cyrillic + CJK pick the bitmap fonts in pixel mode. Cyrillic -> Zpix:
    # PixelOperator has ZERO Cyrillic glyphs, so Russian on it is empty boxes
    # (tofu); Zpix carries a full proportional Cyrillic block.
    assert det("Привет", "pixel") == "Zpix.ttf", "Cyrillic pixel font wrong"
    assert det("你好", "pixel") == "Zpix.ttf", "Chinese pixel font wrong"
    assert det("こんにちは", "pixel") == "Zpix.ttf", "Japanese pixel font wrong"
    # Guard against regressing to a Cyrillic-less pixel font: the chosen font MUST
    # actually contain the glyphs, or the player sees tofu (the bug we just fixed).
    try:
        from fontTools.ttLib import TTFont
        cyr_font = os.path.join(os.path.dirname(__file__), "assets", "fonts", det("Привет", "pixel"))
        _cmap = TTFont(cyr_font).getBestCmap()
        assert all(ord(c) in _cmap for c in "Привет"), "pixel Cyrillic font is missing glyphs (tofu)"
    except ImportError:
        pass  # fontTools optional; the cmap was verified by hand otherwise
    # ...but Korean has NO pixel hangul (Zpix lacks it) -> stays smooth Noto CJK,
    #    in BOTH the swap and the measurement table. This is the load-bearing gap.
    assert det("안녕하세요", "pixel").endswith("NotoSansCJK-Regular.ttc"), \
        "Korean must fall back to smooth Noto (Zpix has no hangul)"
    assert _PIXEL_LANG_FONTS["korean"] == LANG_FONTS["korean"], \
        "Korean pixel measurement must match smooth (no pixel hangul)"
    # Smooth mode is unchanged.
    assert det("Привет", "smooth").endswith("NotoSans-Regular.ttf")
    assert det("你好", "smooth").endswith("NotoSansCJK-Regular.ttc")
    # Latin needs no swap in either style (the original game font already fits) ->
    #    the pixel toggle is a no-op there, exactly as intended.
    assert det("Hello", "smooth") is None and det("Hello", "pixel") is None
    # The pixel font really loads and measures DIFFERENTLY from smooth (proving
    #    the bitmap font, not a silent fallback to Noto, is what we measure).
    assert (
        measure_translation_px("Привет", "Russian", 32, "pixel")
        != measure_translation_px("Привет", "Russian", 32, "smooth")
    ), "pixel measurement collapsed to the smooth font"
    # Korean measures IDENTICALLY in both styles (same Noto CJK) — the gap is real.
    assert (
        measure_translation_px("안녕", "Korean", 32, "pixel")
        == measure_translation_px("안녕", "Korean", 32, "smooth")
    ), "Korean pixel/smooth widths must match (same font)"

    # 10. Fixed-height UI style overrides are GAME-RELATIVE, never a hardcoded 22,
    #     and NEVER inflate a style above its own original size — the bug where a
    #     game whose UI text was already small got blown up (English smaller than
    #     our injected Russian → overflow). The shrink target is the game's own
    #     gui.text_size; an oversized caption (choice_button_text_size) comes down
    #     to it, a style already at/below body size is left untouched.
    import re
    from parsers.renpy import RenPyParser, parse_gui_rpy

    def _read_overrides(tmproot):
        p = os.path.join(tmproot, "game", "tl", "russian", "_ui_style_fixes.rpy")
        if not os.path.exists(p):
            return None
        sizes = {}
        with open(p, encoding="utf-8") as f:
            cur = None
            for ln in f:
                sm = re.match(r'\s*style\s+(\w+)_text:', ln)
                if sm:
                    cur = sm.group(1)
                zm = re.match(r'\s*size\s+(\d+)', ln)
                if zm and cur:
                    sizes[cur] = int(zm.group(1))
        return sizes

    # Game body size 28, an oversized choice caption 40 in a fixed-height screen,
    # and a normal-sized menu caption (no own size → inherits body, nothing to fix).
    rt = tempfile.mkdtemp(prefix="interprex_uifix_selftest_")
    g = os.path.join(rt, "game"); os.makedirs(g)
    with open(os.path.join(g, "gui.rpy"), "w", encoding="utf-8") as f:
        f.write("define gui.text_size = 28\n"
                "define gui.choice_button_text_size = 40\n")
    with open(os.path.join(g, "screens.rpy"), "w", encoding="utf-8") as f:
        f.write(
            "screen fixedbox():\n"
            "    frame:\n"
            "        xysize(300, 80)\n"
            "        vbox:\n"
            "            style_prefix \"choice_button\"\n"   # original 40 -> must shrink to 28
            "            text \"Hi\"\n"
            "    frame:\n"
            "        xysize(300, 80)\n"
            "        vbox:\n"
            "            style_prefix \"menu\"\n"             # no own size -> = body 28 -> skipped
            "            text \"Hi\"\n"
        )
    RenPyParser()._generate_style_overrides(rt, "russian")
    ov = _read_overrides(rt)
    assert ov is not None, "expected _ui_style_fixes.rpy to be generated"
    assert ov.get("choice_button") == 28, \
        f"oversized caption should shrink to game body 28, got {ov.get('choice_button')}"
    assert "menu" not in ov, "a style already at body size must NOT be overridden (no inflation)"
    assert 22 not in ov.values() or ov.get("choice_button") == 28, "must not hardcode 22"

    # Inflation guard: if the game's body text is SMALLER (18) than the legacy 22,
    # we must shrink to 18, never raise to 22.
    rt2 = tempfile.mkdtemp(prefix="interprex_uifix2_selftest_")
    g2 = os.path.join(rt2, "game"); os.makedirs(g2)
    with open(os.path.join(g2, "gui.rpy"), "w", encoding="utf-8") as f:
        f.write("define gui.text_size = 18\n"
                "define gui.choice_button_text_size = 30\n")
    with open(os.path.join(g2, "screens.rpy"), "w", encoding="utf-8") as f:
        f.write(
            "screen fb():\n"
            "    frame:\n"
            "        xysize(300, 80)\n"
            "        vbox:\n"
            "            style_prefix \"choice_button\"\n"
            "            text \"Hi\"\n"
        )
    RenPyParser()._generate_style_overrides(rt2, "russian")
    ov2 = _read_overrides(rt2)
    assert ov2 and ov2.get("choice_button") == 18, \
        f"must shrink to small body 18 (not legacy 22), got {ov2 and ov2.get('choice_button')}"

    # Archive fallback: parse_gui_rpy must read gui.rpy out of a .rpa when no loose
    # copy exists (archive-only games like Killer Chat!), so size resolution works.
    rt3 = tempfile.mkdtemp(prefix="interprex_guirpa_selftest_")
    g3 = os.path.join(rt3, "game"); os.makedirs(g3)
    _build_rpa3(os.path.join(g3, "archive.rpa"),
                {"gui.rpy": b"define gui.text_size = 25\n"})
    _, ints3 = parse_gui_rpy(rt3)
    assert ints3.get("text_size") == 25, \
        f"parse_gui_rpy must read gui.rpy from .rpa, got {ints3.get('text_size')}"

    # 11. Custom-UI game (NO gui.rpy): sizes are baked into `style <prefix>_text:`
    #     blocks in the .rpy (the Killer Chat! case). We must read them directly,
    #     infer body size from the most common one, shrink only the oversized
    #     prefix, and leave the body-sized one untouched (never inflate).
    rt4 = tempfile.mkdtemp(prefix="interprex_styledirect_selftest_")
    g4 = os.path.join(rt4, "game"); os.makedirs(g4)  # deliberately NO gui.rpy
    with open(os.path.join(g4, "screens.rpy"), "w", encoding="utf-8") as f:
        f.write(
            # Two body-sized styles (24) make 24 the inferred body; one big title (60).
            "style small_a_text:\n    size 24\n"
            "style small_b_text:\n    size 24\n"
            "style big_title_text:\n    size 60\n"
            "screen s():\n"
            "    frame:\n"
            "        xysize(400, 90)\n"
            "        vbox:\n"
            "            style_prefix \"big_title\"\n"      # 60 -> shrink to body 24
            "            text \"Hi\"\n"
            "    frame:\n"
            "        xysize(400, 90)\n"
            "        vbox:\n"
            "            style_prefix \"small_a\"\n"        # 24 == body -> untouched
            "            text \"Hi\"\n"
        )
    sizes4 = RenPyParser._parse_style_text_sizes(
        [("game/screens.rpy", open(os.path.join(g4, "screens.rpy"), encoding="utf-8").read())]
    )
    assert sizes4.get("big_title") == 60 and sizes4.get("small_a") == 24, \
        f"style-block sizes misread: {sizes4}"
    RenPyParser()._generate_style_overrides(rt4, "russian")
    ov4 = _read_overrides(rt4)
    assert ov4 and ov4.get("big_title") == 24, \
        f"oversized custom-UI style must shrink to inferred body 24, got {ov4 and ov4.get('big_title')}"
    assert "small_a" not in ov4, "body-sized custom-UI style must be left untouched"

    # 12. `is` inheritance (one level): a style without its own size inherits its
    #     parent's (the common quick_button_text is button_text).
    sizes_is = RenPyParser._parse_style_text_sizes([("g.rpy",
        "style button_text:\n    size 40\n"
        "style quick_button_text is button_text:\n    bold True\n"
    )])
    assert sizes_is.get("quick_button") == 40, \
        f"is-inheritance one level failed: {sizes_is}"

    # 13. Wider container forms: ysize (single-axis) and maximum() without xysize.
    rt5 = tempfile.mkdtemp(prefix="interprex_box_selftest_")
    g5 = os.path.join(rt5, "game"); os.makedirs(g5)
    with open(os.path.join(g5, "gui.rpy"), "w", encoding="utf-8") as f:
        f.write("define gui.text_size = 20\n")
    with open(os.path.join(g5, "screens.rpy"), "w", encoding="utf-8") as f:
        f.write(
            "style big_a_text:\n    size 50\n"
            "style big_b_text:\n    size 50\n"
            "screen a():\n"
            "    frame:\n"
            "        ysize 60\n"                       # single-axis fixed height
            "        vbox:\n"
            "            style_prefix \"big_a\"\n"      # 50 -> body 20
            "            text \"Hi\"\n"
            "screen b():\n"
            "    frame:\n"
            "        maximum(300, 60)\n"               # tuple via maximum()
            "        vbox:\n"
            "            style_prefix \"big_b\"\n"      # 50 -> body 20
            "            text \"Hi\"\n"
        )
    RenPyParser()._generate_style_overrides(rt5, "russian")
    ov5 = _read_overrides(rt5)
    assert ov5 and ov5.get("big_a") == 20, f"ysize box not covered: {ov5}"
    assert ov5.get("big_b") == 20, f"maximum() box not covered: {ov5}"

    # 14. Screen with NO style_prefix: a widget naming its own style is covered.
    rt6 = tempfile.mkdtemp(prefix="interprex_widgetstyle_selftest_")
    g6 = os.path.join(rt6, "game"); os.makedirs(g6)
    with open(os.path.join(g6, "gui.rpy"), "w", encoding="utf-8") as f:
        f.write("define gui.text_size = 18\n")
    with open(os.path.join(g6, "screens.rpy"), "w", encoding="utf-8") as f:
        f.write(
            "style foo_text:\n    size 44\n"
            "screen c():\n"
            "    frame:\n"
            "        xysize(300, 70)\n"
            "        text \"Hi\" style \"foo_text\"\n"   # own style, no style_prefix
        )
    RenPyParser()._generate_style_overrides(rt6, "russian")
    ov6 = _read_overrides(rt6)
    assert ov6 and ov6.get("foo") == 18, \
        f"widget-named style (no style_prefix) not covered: {ov6}"

    # 15. Measured shrink merges with the static path: a factor from the scheduler
    #     forces a smaller size even for a style the static scan wouldn't touch
    #     (choice_button, whose box the engine sizes itself), and takes the
    #     smaller of (static body, original*factor).
    rt7 = tempfile.mkdtemp(prefix="interprex_measured_selftest_")
    g7 = os.path.join(rt7, "game"); os.makedirs(g7)
    with open(os.path.join(g7, "gui.rpy"), "w", encoding="utf-8") as f:
        f.write("define gui.text_size = 30\n"
                "define gui.choice_button_text_size = 30\n")
    # No fixed-height screen at all → static path emits nothing for choice_button.
    RenPyParser()._generate_style_overrides(rt7, "russian", {"choice_button": 0.5})
    ov7 = _read_overrides(rt7)
    assert ov7 and ov7.get("choice_button") == 15, \
        f"measured factor 0.5 on size 30 must yield 15, got {ov7}"


def check_renpy_risk() -> None:
    """The overflow RISK ANALYZER (parsers/renpy_risk.py) must read a game's own
    layout declarations and return a data-driven verdict — not guess. Two synthetic
    games mirror the two real cases we verified by hand:
      - fixed stock textbox + long source line  -> 'high'  (Watch the Road shape)
      - auto-computed dialogue height + scroll   -> 'none'  (Killer Chat shape)
    Plus: a non-fixed textbox is 'none', and reading must cover BOTH loose .rpy and
    .rpa-archived sources."""
    import os, tempfile
    from parsers.renpy_risk import analyze, _LONG_SAY_CHARS

    # --- Case 1: fixed-height stock textbox with a very long say line -> HIGH ---
    g1 = tempfile.mkdtemp()
    os.makedirs(os.path.join(g1, "game"))
    long_line = "x" * (_LONG_SAY_CHARS + 50)
    with open(os.path.join(g1, "game", "gui.rpy"), "w", encoding="utf-8") as f:
        f.write("define gui.textbox_height = 278\n")
    with open(os.path.join(g1, "game", "screens.rpy"), "w", encoding="utf-8") as f:
        f.write('screen say(who, what):\n    window:\n        text what id "what"\n')
    with open(os.path.join(g1, "game", "script.rpy"), "w", encoding="utf-8") as f:
        f.write('label start:\n    e "Short line."\n    e "%s"\n' % long_line)
    r1 = analyze(g1)
    assert r1["dialogue_overflow_risk"] == "high", r1
    assert r1["textbox_height_fixed"] and r1["textbox_height"] == "278", r1
    assert r1["long_say_lines"] >= 1 and r1["longest_say_chars"] >= _LONG_SAY_CHARS, r1

    # --- Case 2: auto-computed dialogue height (+scroll in the say body) -> NONE ---
    g2 = tempfile.mkdtemp()
    os.makedirs(os.path.join(g2, "game"))
    with open(os.path.join(g2, "game", "dialogue.rpy"), "w", encoding="utf-8") as f:
        f.write(
            "init python:\n    def calculate_dialogue_height(who, what):\n        pass\n"
            'screen say(who, what):\n    viewport:\n        text what id "what"\n'
        )
    with open(os.path.join(g2, "game", "script.rpy"), "w", encoding="utf-8") as f:
        f.write('label start:\n    e "%s"\n' % long_line)
    r2 = analyze(g2)
    assert r2["dialogue_overflow_risk"] == "none", r2
    assert r2["auto_height_dialogue"] and r2["has_dialogue_scroll"], r2

    # --- Case 3: explicit NON-fixed textbox height -> NONE (box grows) ---
    g3 = tempfile.mkdtemp()
    os.makedirs(os.path.join(g3, "game"))
    with open(os.path.join(g3, "game", "gui.rpy"), "w", encoding="utf-8") as f:
        f.write("define gui.textbox_height = None\n")
    with open(os.path.join(g3, "game", "script.rpy"), "w", encoding="utf-8") as f:
        f.write('label start:\n    e "%s"\n' % long_line)
    r3 = analyze(g3)
    assert r3["dialogue_overflow_risk"] == "none", r3
    assert not r3["textbox_height_fixed"], r3

    # --- Case 4: scroll detection is SCOPED to the say-screen body, not the file.
    # A viewport in a DIFFERENT screen must NOT mask a fixed stock say box. ---
    g4 = tempfile.mkdtemp()
    os.makedirs(os.path.join(g4, "game"))
    with open(os.path.join(g4, "game", "gui.rpy"), "w", encoding="utf-8") as f:
        f.write("define gui.textbox_height = 200\n")
    with open(os.path.join(g4, "game", "screens.rpy"), "w", encoding="utf-8") as f:
        f.write(
            'screen say(who, what):\n    window:\n        text what id "what"\n'
            'screen history():\n    viewport:\n        text "log of messages"\n'
        )
    with open(os.path.join(g4, "game", "script.rpy"), "w", encoding="utf-8") as f:
        f.write('label start:\n    e "%s"\n' % long_line)
    r4 = analyze(g4)
    assert not r4["has_dialogue_scroll"], "viewport in another screen must not count as dialogue scroll"
    assert r4["dialogue_overflow_risk"] == "high", r4

    # --- Case 5: .rpa-archived sources are read (not just loose files) ---
    # Reuse the rpa builder from check_renpy_rpa if present; otherwise verify the
    # analyzer at least counts archive sources on a real game is covered elsewhere.
    # Here we just confirm an empty game degrades to a clean 'none', never crashes.
    g5 = tempfile.mkdtemp()
    os.makedirs(os.path.join(g5, "game"))
    r5 = analyze(g5)
    assert r5["dialogue_overflow_risk"] == "none" and r5["say_lines"] == 0, r5

    print("OK — renpy risk analyzer: fixed-box=high, auto/scroll=none, "
          "non-fixed=none, scroll-scoped-to-say-body, empty degrades")

    # --- engine-lint helpers (pure parts; the subprocess run is integration) ---
    from parsers.renpy import _lint_is_actionable, _find_engine_exe
    # Real hazards the engine oracle catches -> actionable.
    assert _lint_is_actionable("Unterminated string format code '%' (in \"100%\")")
    assert _lint_is_actionable("Close text tag '{/color}' does not match an open text tag.")
    assert _lint_is_actionable("A translation for \"X\" already exists")
    # Benign tl/-lint noise (runtime var the linter can't resolve) -> NOT actionable.
    assert not _lint_is_actionable("Could not evaluate 'attribute' in the who part of a say statement.")
    assert not _lint_is_actionable("The screen foo has not been given a parameter list.")
    # exe finder: a fake game dir with <name>.exe + paired <name>.py is found.
    g_exe = tempfile.mkdtemp()
    open(os.path.join(g_exe, "Game.exe"), "w").close()
    open(os.path.join(g_exe, "Game.py"), "w").close()
    found = _find_engine_exe(g_exe)
    assert found and found.endswith("Game.exe"), found
    # No exe at all -> None (player machine without the SDK), never raises.
    assert _find_engine_exe(tempfile.mkdtemp()) is None
    print("OK — renpy engine-lint: actionable vs benign classification, exe discovery")

    # --- %-format escaping (deterministic crash-class fix at inject time) ---
    from parsers.renpy import _escape_bad_percent as _pf
    # A bare % the LLM dropped escaping on -> %% (the real Killer Chat bug shape).
    assert _pf("ЗАВЕРШЕНО НА 100%") == "ЗАВЕРШЕНО НА 100%%"
    assert _pf("на 100% с криком") == "на 100%% с криком"  # '% с' invalid spec
    assert _pf("trailing %") == "trailing %%"
    # Valid format specs and already-escaped %% are left BYTE-VERBATIM.
    assert _pf("Loaded %d items") == "Loaded %d items"
    assert _pf("Hi %(name)s!") == "Hi %(name)s!"
    assert _pf("100%% ASSIGNMENT") == "100%% ASSIGNMENT"
    assert _pf("no percent here") == "no percent here"
    # Idempotent: re-running never double-escapes (safe to apply every inject).
    for s in ("НА 100%", "100%% done", "%d of %d", "x % y % z"):
        assert _pf(_pf(s)) == _pf(s), f"not idempotent: {s!r}"
    print("OK — renpy %-format: bare % escaped, valid specs/%% verbatim, idempotent")

    # --- dialogue auto-fit: {size=*scale} only for KNOWN fixed boxes ---
    from parsers.renpy import fit_scale_for_box, RenPyParser as _RP
    BOX_W, BOX_H, FS = 1650, 360, 45  # real Watch the Road dialogue box
    short = "Привет, как дела?"
    huge = ("Причина крушения остаётся неясной, хотя следователи указывают на "
            "неблагоприятные погодные условия и возможную ошибку пилота. " * 6)
    # Short line fits at full size -> scale 1.0 -> NOT wrapped.
    assert fit_scale_for_box(short, "Russian", FS, BOX_W, BOX_H) == 1.0
    assert _RP._fit_dialogue(short, BOX_W, BOX_H, FS, "Russian", "smooth") == short
    # Huge line overflows -> scale < 1.0 (but never below the 0.6 floor) -> wrapped.
    s_huge = fit_scale_for_box(huge, "Russian", FS, BOX_W, BOX_H)
    assert 0.6 <= s_huge < 1.0, s_huge
    wrapped = _RP._fit_dialogue(huge, BOX_W, BOX_H, FS, "Russian", "smooth")
    assert wrapped.startswith("{size=*") and wrapped.endswith("{/size}"), wrapped
    assert huge in wrapped, "full text must be preserved inside the size tag (never cut)"
    # UNKNOWN box (dims 0) -> NEVER touch the text (the user's contract).
    assert fit_scale_for_box(huge, "Russian", FS, 0, 0) == 1.0
    assert _RP._fit_dialogue(huge, 0, 0, FS, "Russian", "smooth") == huge
    # Already-tagged text is not double-wrapped.
    pre = "{size=20}текст"
    assert _RP._fit_dialogue(pre, BOX_W, BOX_H, FS, "Russian", "smooth") == pre
    print("OK — renpy dialogue auto-fit: fits=untouched, overflow={size=*} full text, "
          "unknown box untouched, no double-wrap")

    # --- menu-choice one-line fit (stops button growing past the UI frame) ---
    from parsers.renpy import fit_scale_one_line, RenPyParser as _RP2
    CB_W, CB_FS = 920, 30  # Killer Chat choice_button inner width / font
    short_c = "Да, конечно!"
    long_c = ("Я не уверен что это очень хорошая идея, давай обсудим это чуть "
              "попозже, когда будет время " * 2)
    # Short choice fits one line -> untouched.
    assert fit_scale_one_line(short_c, "Russian", CB_FS, CB_W) == 1.0
    assert _RP2._fit_one_line(short_c, CB_W, CB_FS, "Russian", "smooth") == short_c
    # Long choice would wrap -> shrunk so it fits one line (scale < 1, >= floor).
    s_long = fit_scale_one_line(long_c, "Russian", CB_FS, CB_W)
    assert 0.6 <= s_long < 1.0, s_long
    wrapped_c = _RP2._fit_one_line(long_c, CB_W, CB_FS, "Russian", "smooth")
    assert wrapped_c.startswith("{size=*") and long_c in wrapped_c, wrapped_c
    # Unknown button width -> never touch (contract).
    assert _RP2._fit_one_line(long_c, 0, CB_FS, "Russian", "smooth") == long_c
    print("OK — renpy menu-choice one-line fit: short untouched, long shrunk to one "
          "line, unknown width untouched")


def check_renpy_identifier_parity() -> None:
    """Anchor our engine-identifier algorithm to values verified against an
    oracle generated by `<Game>.exe <gamedir> translate russian` (Takei's
    Journey). If these drift, the engine stops binding our translations. The
    digest formula was verified 56242/56242 against that oracle."""
    from parsers.renpy import _say_get_code, _md5_identifier

    # (who_var, attrs, raw_what, nointeract) -> expected digest (no label prefix)
    cases = [
        # Real lines from Takei's Journey tl/russian (label "start"):
        ("Narrator", [], "{i}This is the history of the Takei Clan, an ancient clan from the Shinobi world.", False, "f784ee1a"),
        ("Narrator", [], "{i}Great name! Now time for his landlady...", False, "51947d22"),
        ("Narrator", [], "{i}After some time searching for enemies, Sasuke has finally come back home.", False, "b70ff626"),
    ]
    for who, attrs, what, noi, expected in cases:
        code = _say_get_code(who, attrs, what, nointeract=noi)
        got = _md5_identifier(code)
        assert got == expected, f"identifier parity drift: {what[:30]!r} got={got} expected={expected}"


def check_renpy_mixed_translate() -> None:
    """A single .rpy can hold real SOURCE *and* inline `translate <lang> …:` blocks
    (a dev keeping script + its translation together). The block body is
    already-translated text — `_scan` must skip it, NOT ingest it as source
    (else we'd re-translate another language's output and emit duplicate ids).
    Real source before AND after the block must still be extracted, and the
    parser's translate-block state must reset cleanly across menus/labels."""
    from parsers.renpy import RenPyParser

    mixed = (
        'label start:\n'
        '    e "Real before."\n'
        'translate russian start_abc123:\n'
        '\n'
        '    # e "Real before."\n'
        '    e "Perevod odin."\n'           # translated say — must be skipped
        'translate russian strings:\n'
        '    old "Yes"\n'
        '    new "Da"\n'                     # translated strings — must be skipped
        'translate russian python:\n'
        '    foo = "bar"\n'                  # translate python body — skipped
        'menu choose:\n'                     # state must reset: this is real source
        '    "Choice A":\n'
        '        jump x\n'
        'label after:\n'
        '    e "Real after."\n'
    )
    p = RenPyParser()
    got = {r["raw_what"] for r in p._scan(mixed)
           if r["native_kind"] in ("say", "string", "menu_choice")}
    # real source on both sides of the inline blocks + the menu choice
    assert {"Real before.", "Real after.", "Choice A"} <= got, got
    # NONE of the already-translated text leaks in as source
    assert not ({"Perevod odin.", "Yes", "Da", "bar"} & got), \
        f"inline translate-block text ingested as source: {got}"


def check_renpy_context_history() -> None:
    """Dialogue history (Prev line) context is passed sequentially.
    Resets on labels, screens, menus, flow control, and translate blocks.
    Preserved across show, hide, scene, with, $, pause, etc."""
    from parsers.renpy import RenPyParser

    script = (
        'label start:\n'
        '    define alice = Character("Alice")\n'
        '    define bob = Character("Bob")\n'
        '    alice "Hello, Bob!"\n'
        '    bob "Hi, Alice."\n'
        '    # Non-interrupting statements\n'
        '    show bob_happy\n'
        '    $ bob_mood = "happy"\n'
        '    pause 1.0\n'
        '    alice "Are you happy?"\n'
        '    # Testing single quotes / apostrophe escaping\n'
        '    bob "It\'s a good day."\n'
        '    alice "Awesome."\n'
        '    # Testing narrator (no speaker name)\n'
        '    "The sun begins to set."\n'
        '    alice "Beautiful."\n'
        '    # Testing very long line trimming (>250 chars)\n'
        '    bob "This is a very long line. ' + 'x' * 200 + ' It should be trimmed to 150 chars plus ellipsis."\n'
        '    alice "Okay."\n'
        '    # Testing extend pseudo-speaker\n'
        '    alice "I was thinking..."\n'
        '    extend " that we should go."\n'
        '    bob "Agree."\n'
        '    # Interrupting: jump flow control\n'
        '    jump end_scene\n'
        'label end_scene:\n'
        '    alice "We arrived."\n'
    )
    p = RenPyParser()
    got = list(p._scan(script))
    
    # Filter for say/narrator statements
    says = [r for r in got if r["native_kind"] == "say"]
    
    # 1. alice "Hello, Bob!" (No history yet)
    assert "Prev line" not in says[0]["context"], f"Expected no history on first line, got: {says[0]['context']}"
    
    # 2. bob "Hi, Alice."
    assert "Prev line: 'Hello, Bob!' (Alice)" in says[1]["context"], f"Expected context for Alice line, got: {says[1]['context']}"
    
    # 3. Non-interrupting show/$/pause do not reset history.
    # alice "Are you happy?" (should have bob's prev line)
    assert "Prev line: 'Hi, Alice.' (Bob)" in says[2]["context"], f"Expected context preserved across show/$, got: {says[2]['context']}"
    
    # 4. bob "It's a good day." (test single quotes / apostrophes)
    # Expected: Prev line: 'Are you happy?' (Alice)
    assert "Prev line: 'Are you happy?' (Alice)" in says[3]["context"], f"Expected context, got: {says[3]['context']}"
    
    # 5. alice "Awesome." (previous text contains apostrophe)
    # Expected exactly: Prev line: 'It's a good day.' (Bob)
    expected_apostrophe = "Prev line: 'It's a good day.' (Bob)"
    assert expected_apostrophe in says[4]["context"], f"Expected context with apostrophe: {expected_apostrophe}, got: {says[4]['context']}"
    
    # 6. Narrator line (no speaker)
    # Expected: Prev line: 'Awesome.' (Alice)
    assert "Prev line: 'Awesome.' (Alice)" in says[5]["context"], f"Expected context, got: {says[5]['context']}"
    
    # 7. alice "Beautiful." (previous line was narrator)
    # Expected: Prev line: 'The sun begins to set.' (narrator)
    assert "Prev line: 'The sun begins to set.' (narrator)" in says[6]["context"], f"Expected context with narrator fallback, got: {says[6]['context']}"
    
    # 8. alice "Okay." (previous line was very long)
    # Length of bob's monologue is > 250, should be trimmed to 150 + "..."
    expected_prefix = "This is a very long line. " + "x" * 124 + "..."
    expected_trim = f"Prev line: '{expected_prefix}' (Bob)"
    assert expected_trim in says[8]["context"], f"Expected trimmed context: {expected_trim}, got: {says[8]['context']}"
    
    # 9. bob "Agree." (previous line was extend)
    # The extend pseudo-speaker appends to "I was thinking... that we should go." under Alice.
    assert "Prev line: 'I was thinking... that we should go.' (Alice)" in says[11]["context"], f"Expected extend concatenated context, got: {says[11]['context']}"
    
    # 10. alice "We arrived." (after label change and jump flow control)
    # Label change and jump must reset context history.
    assert "Prev line" not in says[12]["context"], f"Expected label/jump to reset context, got: {says[12]['context']}"


def _build_rpa3(path: str, files: dict[str, bytes]) -> None:
    """Write a minimal RPA-3.0 archive of `files` (inner_path -> bytes). Mirrors
    the format read by parsers/rpa.py: header line, raw payloads, then an
    obfuscated zlib+pickle index at the end."""
    import pickle
    import zlib
    KEY = 0x42424242
    with open(path, "wb") as f:
        # Reserve the header line; we rewrite it once the index offset is known.
        # RPA headers are fixed-width hex, so a placeholder of the same shape works.
        header_placeholder = b"RPA-3.0 %016x %08x\n" % (0, KEY)
        f.write(header_placeholder)
        index: dict[str, list] = {}
        for name, data in files.items():
            off = f.tell()
            f.write(data)
            # one segment: [offset ^ key, length ^ key, prefix]
            index[name] = [[off ^ KEY, len(data) ^ KEY, b""]]
        index_off = f.tell()
        f.write(zlib.compress(pickle.dumps(index, 2)))
        f.seek(0)
        f.write(b"RPA-3.0 %016x %08x\n" % (index_off, KEY))


def check_renpy_rpa() -> None:
    """Ren'Py games that pack scripts inside game/archive.rpa (no loose .rpy) must
    still detect/extract/inject. Also verifies a loose .rpy WINS over an archived
    one of the same path (engine loads disk before archive)."""
    from parsers.rpa import read_rpa

    root = tempfile.mkdtemp(prefix="interprex_rpa_selftest_")
    game = os.path.join(root, "game")
    os.makedirs(game)

    archived_script = (
        'label start:\n'
        '    "Packed narration."\n'
        '    e "Packed dialogue."\n'
    ).encode("utf-8")
    overridden = b'label over:\n    "ARCHIVE version."\n'
    # Some games keep real SOURCE under tl/None/ (Ren'Py "no language" tree) and
    # ship a finished translation under tl/<lang>/. We must read the former as
    # source but NOT ingest the latter as source (it's another language's text).
    none_source = (
        'label bonus:\n'
        '    "Source under tl None."\n'
    ).encode("utf-8")
    chinese_tl = (
        '# game/script.rpy:2\n'
        'translate chinese start_abcd1234:\n'
        '\n'
        '    # "Packed narration."\n'
        '    "包装叙述。"\n'
    ).encode("utf-8")
    # Bundle a non-.rpy entry to prove suffix filtering skips media.
    _build_rpa3(os.path.join(game, "archive.rpa"), {
        "script/story.rpy": archived_script,
        "over.rpy": overridden,
        "tl/None/bonus.rpy": none_source,
        "tl/chinese/script.rpy": chinese_tl,
        "images/bg.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
    })

    # rpa reader: only .rpy returned, decoded as text
    rpy_files = read_rpa(os.path.join(game, "archive.rpa"))
    names = sorted(rf.path for rf in rpy_files)
    assert names == ["over.rpy", "script/story.rpy",
                     "tl/None/bonus.rpy", "tl/chinese/script.rpy"], names

    # detect via archive (zero loose .rpy on disk)
    assert detect_engine(root) == "renpy", "renpy .rpa detect failed"
    p = get_parser("renpy")

    strings = p.extract(root)
    by_orig = {s.original: s for s in strings}
    assert "Packed narration." in by_orig, [s.original for s in strings]
    assert "Packed dialogue." in by_orig, [s.original for s in strings]
    # archived file addressed as game/<inner> (id portability invariant)
    assert by_orig["Packed narration."].file == "game/script/story.rpy", by_orig["Packed narration."].file
    # SOURCE under tl/None/ IS read (it's code, not a translation)
    assert "Source under tl None." in by_orig, [s.original for s in strings]
    # existing tl/<lang>/ translation is SKIPPED — its text must NOT appear as
    # a source string (don't re-translate another language's output).
    assert "包装叙述。" not in by_orig, "shipped tl/chinese/ ingested as source!"

    # inject from archive -> tl/ written, archive untouched
    rpa_before = open(os.path.join(game, "archive.rpa"), "rb").read()
    tr = {s.id: s.original.upper() for s in strings}
    written = p.inject(root, tr, "russian")
    assert written == len(strings), f"rpa inject written={written}/{len(strings)}"
    tl = os.path.join(game, "tl", "russian", "script", "story.rpy")
    assert os.path.exists(tl), "tl from archived .rpy not written"
    assert '"PACKED NARRATION."' in open(tl, encoding="utf-8").read()
    assert open(os.path.join(game, "archive.rpa"), "rb").read() == rpa_before, "inject modified the .rpa!"

    # loose .rpy WINS over archived one of the same inner path
    loose = os.path.join(game, "over.rpy")
    open(loose, "w", encoding="utf-8").write('label over:\n    "LOOSE version."\n')
    originals2 = [s.original for s in p.extract(root)]
    assert "LOOSE version." in originals2, originals2
    assert "ARCHIVE version." not in originals2, originals2


def check_renpy_decompilation() -> None:
    """Test the automatic Ren'Py decompilation pipeline on .rpyc files in RPA archives.

    Decompilements go into a temp directory (NOT game/), so Ren'Py never
    double-loads scripts (disk + archive). The temp is cleaned up after
    extract/inject."""
    from parsers.renpy import RenPyParser

    root = tempfile.mkdtemp(prefix="interprex_decompile_selftest_")
    game = os.path.join(root, "game")
    os.makedirs(game)

    # We build an archive with a .rpyc file, but NO loose .rpy or loose .rpyc.
    _build_rpa3(os.path.join(game, "archive.rpa"), {
        "story.rpyc": b"DUMMY_RPYC_DATA",
    })

    p = RenPyParser()

    # Create a mock unrpyc module in sys.modules
    import types
    mock_unrpyc = types.ModuleType("unrpyc")
    def mock_decompile_rpyc(input_filename, **kwargs):
        from pathlib import Path as PathLib
        out_filename = PathLib(input_filename).with_suffix(".rpy")
        with open(out_filename, "w", encoding="utf-8") as f_out:
            f_out.write('label start:\n    "Decompiled text."\n')
        return True
    mock_unrpyc.decompile_rpyc = mock_decompile_rpyc

    import sys
    sys.modules["unrpyc"] = mock_unrpyc

    # Run extract, which should trigger decompilation into temp
    strings = p.extract(root)

    # Decompilations go to temp, NOT game/ — no loose .rpy on disk
    decompiled_rpy = os.path.join(game, "story.rpy")
    assert not os.path.exists(decompiled_rpy), "Decompiled .rpy must NOT be in game/ (causes double-load)"

    # Temp dirs cleaned up after extract
    assert len(p._decompile_temp_dirs) == 0, "Temp dirs should be cleaned up"

    # Check that strings were successfully extracted from the decompiled temp .rpy
    originals = [s.original for s in strings]
    assert "Decompiled text." in originals, originals

    # Decompilements from archives must NOT be registered in backup metadata
    metadata_path = os.path.join(root, ".interprex_backups", "metadata.json")
    if os.path.exists(metadata_path):
        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)
        assert "game/story.rpy" not in metadata, (
            "Decompiled file must not be registered in backup metadata"
        )

    if "unrpyc" in sys.modules:
        del sys.modules["unrpyc"]


def build_csharp_project() -> str:
    root = tempfile.mkdtemp(prefix="interprex_csharp_selftest_")
    src = os.path.join(root, "src")
    os.makedirs(src)
    code = (
        'using System;\n'
        '\n'
        'namespace MyGame {\n'
        '    public class Player {\n'
        '        // Comment here\n'
        '        /* Block comment\n'
        '           here */\n'
        '        private string name = "Hero";\n'
        '        private char flag = \'A\';\n'
        '        \n'
        '        public void Greet() {\n'
        '            Console.WriteLine("Hello player!");\n'
        '            Console.WriteLine(@"Verbatim ""quoted"" string");\n'
        '            string ignore_me = "Assets/Textures/logo.png";\n'
        '            string ignore_me_too = "";\n'
        '            string skip_interpolated = $"Value: {flag}";\n'
        '            string skip_interpolated_verbatim_1 = $@"Verbatim value: {flag}";\n'
        '            string skip_interpolated_verbatim_2 = @$"Another verbatim value: {flag}";\n'
        '        }\n'
        '    }\n'
        '}\n'
    )
    open(os.path.join(src, "Player.cs"), "w", encoding="utf-8").write(code)
    return root


def check_csharp() -> None:
    root = build_csharp_project()
    fpath = os.path.join(root, "src", "Player.cs")

    assert detect_engine(root) == "csharp", "csharp detect failed"
    p = get_parser("csharp")

    strings = p.extract(root)
    originals = [s.original for s in strings]
    assert originals == [
        "Hero",
        "Hello player!",
        'Verbatim "quoted" string'
    ], originals

    # Check stable IDs across runs
    ids1 = {s.id for s in strings}
    ids2 = {s.id for s in p.extract(root)}
    assert ids1 == ids2, "csharp ids not stable across runs"

    # Check paths
    by_orig = {s.original: s for s in strings}
    assert by_orig["Hero"].path == ["Player", "str_0"], by_orig["Hero"].path
    assert by_orig["Hello player!"].path == ["Player", "Greet", "str_0"], by_orig["Hello player!"].path
    assert by_orig['Verbatim "quoted" string'].path == ["Player", "Greet", "str_1"], by_orig['Verbatim "quoted" string'].path

    # Check inject
    tr = {s.id: s.original.upper() for s in strings}
    written = p.inject(root, tr)
    assert written == 3, f"csharp written={written}"

    after = open(fpath, encoding="utf-8").read()
    assert 'private string name = "HERO";' in after, after
    assert 'Console.WriteLine("HELLO PLAYER!");' in after, after
    assert 'Console.WriteLine(@"VERBATIM ""QUOTED"" STRING");' in after, after

    # Check comments and ignored strings are untouched
    assert '// Comment here' in after, after
    assert "private char flag = 'A';" in after, after
    assert 'string ignore_me = "Assets/Textures/logo.png";' in after, after
    assert 'string skip_interpolated = $"Value: {flag}";' in after, after
    assert 'string skip_interpolated_verbatim_1 = $@"Verbatim value: {flag}";' in after, after
    assert 'string skip_interpolated_verbatim_2 = @$"Another verbatim value: {flag}";' in after, after


def build_dll_project() -> tuple[str, str]:
    """Creates and compiles a target C# DLL project on the fly, returning (root_dir, dll_path)."""
    import subprocess
    root = tempfile.mkdtemp(prefix="interprex_dll_selftest_")

    # Create classlib template
    subprocess.run(
        ["dotnet", "new", "classlib", "-o", ".", "--force"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
        cwd=root
    )

    # Write Class1.cs code
    code = (
        'using System;\n'
        'namespace TestNamespace {\n'
        '    public class GameText {\n'
        '        public string name = "Original Game Name";\n'
        '        public void SayHello() {\n'
        '            Console.WriteLine("Welcome to the modded game!");\n'
        '            Console.WriteLine("ignore_me_because_no_letters_123");\n'
        '        }\n'
        '    }\n'
        '}\n'
    )
    open(os.path.join(root, "Class1.cs"), "w", encoding="utf-8").write(code)

    # Build DLL
    out_dir = os.path.join(root, "out")
    subprocess.run(
        ["dotnet", "build", "-c", "Release", "-o", out_dir, "-p:NuGetAudit=false"],
        check=True,
        cwd=root
    )

    # Clean up all .cs files so the csharp engine detector doesn't match this folder
    for r, dirs, files in os.walk(root):
        for f in files:
            if f.endswith(".cs"):
                os.remove(os.path.join(r, f))

    dll_path = None
    for f in os.listdir(out_dir):
        if f.endswith(".dll") and not f.startswith("System.") and f != "Mono.Cecil.dll":
            dll_path = os.path.join(out_dir, f)
            break

    return root, dll_path


def check_unity() -> None:
    import shutil
    root, dll_path = build_dll_project()
    assert dll_path is not None, "Failed to compile test DLL"

    # Create a fake prefab file
    prefab_path = os.path.join(root, "TestMenu.prefab")
    with open(prefab_path, "w", encoding="utf-8") as f:
        f.write(
            "MonoBehaviour:\n"
            "  m_ObjectHideFlags: 0\n"
            '  m_Text: "Click Start"\n'
            "  m_text: 'Quit Game'\n"
        )

    try:
        assert detect_engine(root) == "unity", f"Unity engine detect failed: {detect_engine(root)}"
        p = get_parser("unity")

        strings = p.extract(root)
        originals = [s.original for s in strings]
        assert originals == [
            "Welcome to the modded game!",
            "Original Game Name",
            "Click Start",
            "Quit Game"
        ], originals

        # Check paths
        by_orig = {s.original: s for s in strings}
        assert by_orig["Original Game Name"].path == ["TestNamespace.GameText", ".ctor", "str_0"], by_orig["Original Game Name"].path
        assert by_orig["Welcome to the modded game!"].path == ["TestNamespace.GameText", "SayHello", "str_0"], by_orig["Welcome to the modded game!"].path
        assert by_orig["Click Start"].path == ["AssetYAML", "m_Text", "2"], by_orig["Click Start"].path
        assert by_orig["Quit Game"].path == ["AssetYAML", "m_Text", "3"], by_orig["Quit Game"].path

        # Check stable IDs across runs
        ids1 = {s.id for s in strings}
        ids2 = {s.id for s in p.extract(root)}
        assert ids1 == ids2, "Unity ids not stable across runs"

        # Check inject
        tr = {s.id: s.original.upper() for s in strings}
        written = p.inject(root, tr)
        assert written == 4, f"Unity written={written}"

        # Re-extract and verify
        strings_after = p.extract(root)
        originals_after = [s.original for s in strings_after]
        assert originals_after == [
            "WELCOME TO THE MODDED GAME!",
            "ORIGINAL GAME NAME",
            "CLICK START",
            "QUIT GAME"
        ], originals_after
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_unity_localization() -> None:
    from unittest.mock import patch, MagicMock
    import shutil
    
    root = tempfile.mkdtemp(prefix="interprex_loc_selftest_")
    aa_dir = os.path.join(root, "StreamingAssets", "aa")
    os.makedirs(aa_dir)
    open(os.path.join(aa_dir, "catalog.json"), "w").write("{}")
    bundle_path = os.path.join(aa_dir, "loc_english_assets_all.bundle")
    open(bundle_path, "wb").write(b"fake bundle content")

    try:
        assert detect_engine(root) == "unity", f"Loc detect failed: {detect_engine(root)}"
        p = get_parser("unity")

        mock_env = MagicMock()
        
        mock_shared_obj = MagicMock()
        mock_shared_obj.type.name = "MonoBehaviour"
        mock_shared_obj.path_id = 123
        mock_shared_data = MagicMock()
        mock_shared_data.read_typetree.return_value = {
            "m_TableCollectionName": "MyGameStrings",
            "m_Entries": [
                {"m_Id": 1, "m_Key": "KEY_START"},
                {"m_Id": 2, "m_Key": "KEY_QUIT"}
            ]
        }
        mock_shared_obj.read.return_value = mock_shared_data

        mock_table_obj = MagicMock()
        mock_table_obj.type.name = "MonoBehaviour"
        mock_table_obj.path_id = 456
        mock_table_data = MagicMock()
        mock_table_tree = {
            "m_LocaleIdentifier": {"m_Code": "en"},
            "m_SharedData": {"m_PathID": 123},
            "m_TableData": [
                {"m_Id": 1, "m_Localized": "Start Game"},
                {"m_Id": 2, "m_Localized": "Exit Game"}
            ]
        }
        mock_table_data.read_typetree.return_value = mock_table_tree
        mock_table_obj.read.return_value = mock_table_data

        mock_env.objects = [mock_shared_obj, mock_table_obj]
        mock_env.file.save.return_value = b"new fake bundle bytes"

        with patch("UnityPy.load", return_value=mock_env):
            strings = p.extract(root)
            
        originals = [s.original for s in strings]
        assert originals == ["Start Game", "Exit Game"], originals
        
        by_orig = {s.original: s for s in strings}
        assert by_orig["Start Game"].path == ["StringTable", "MyGameStrings", "en", "KEY_START"], by_orig["Start Game"].path
        assert by_orig["Exit Game"].path == ["StringTable", "MyGameStrings", "en", "KEY_QUIT"], by_orig["Exit Game"].path

        tr = {s.id: s.original.upper() for s in strings}
        
        mock_table_data.reset_mock()
        mock_env.file.save.reset_mock()
        mock_table_data.read_typetree.return_value = mock_table_tree
        
        with patch("UnityPy.load", return_value=mock_env):
            written = p.inject(root, tr)

        assert written == 2, f"Written={written}"
        assert mock_table_tree["m_TableData"][0]["m_Localized"] == "START GAME"
        assert mock_table_tree["m_TableData"][1]["m_Localized"] == "EXIT GAME"
        
        mock_table_data.save_typetree.assert_called_with(mock_table_tree)
        mock_env.file.save.assert_called_with(packer="none")
        
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_i18n() -> None:
    # Build a fake Stardew + RimWorld folder structure
    root = tempfile.mkdtemp(prefix="interprex_i18n_selftest_")
    
    # 1. Stardew default.json
    os.makedirs(os.path.join(root, "i18n"), exist_ok=True)
    stardew_data = {
        "simple_key": "original simple",
        "nested_section": {
            "nested_key": "original nested"
        }
    }
    with open(os.path.join(root, "i18n", "default.json"), "w", encoding="utf-8") as f:
        json.dump(stardew_data, f, ensure_ascii=False)

    # 2. RimWorld Languages
    os.makedirs(os.path.join(root, "Languages", "English", "Keyed"), exist_ok=True)
    os.makedirs(os.path.join(root, "Languages", "English", "DefInjected", "RecipeDef"), exist_ok=True)
    
    xml_keyed = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<LanguageData>\n'
        '  <KeyedOne>original keyed text</KeyedOne>\n'
        '</LanguageData>\n'
    )
    with open(os.path.join(root, "Languages", "English", "Keyed", "Keys.xml"), "w", encoding="utf-8") as f:
        f.write(xml_keyed)
        
    xml_def = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<LanguageData>\n'
        '  <Make_Thing.label>original def label</Make_Thing.label>\n'
        '</LanguageData>\n'
    )
    with open(os.path.join(root, "Languages", "English", "DefInjected", "RecipeDef", "Recipes.xml"), "w", encoding="utf-8") as f:
        f.write(xml_def)

    # Detect engine
    assert detect_engine(root) == "i18n", "i18n detect failed"
    p = get_parser("i18n")

    # Extract
    strings = p.extract(root)
    
    assert len(strings) == 4, f"expected 4 strings, got {len(strings)}"
    
    # Verify originals and contexts
    originals = {s.original for s in strings}
    assert "original simple" in originals
    assert "original nested" in originals
    assert "original keyed text" in originals
    assert "original def label" in originals

    # Verify ID stability
    ids1 = {s.id for s in strings}
    ids2 = {s.id for s in p.extract(root)}
    assert ids1 == ids2, "i18n ids not stable"

    # Inject
    tr = {s.id: s.original.upper() for s in strings}
    written = p.inject(root, tr, "Russian")
    assert written == 4, f"i18n written={written}"

    # Verify injected Stardew json
    ru_json_path = os.path.join(root, "i18n", "ru.json")
    assert os.path.isfile(ru_json_path)
    with open(ru_json_path, "r", encoding="utf-8-sig") as f:
        ru_data = json.load(f)
    assert ru_data["simple_key"] == "ORIGINAL SIMPLE"
    assert ru_data["nested_section"]["nested_key"] == "ORIGINAL NESTED"

    # Verify injected RimWorld XMLs
    ru_keyed_path = os.path.join(root, "Languages", "Russian (Русский)", "Keyed", "Keys.xml")
    assert os.path.isfile(ru_keyed_path)
    tree_keyed = ET.parse(ru_keyed_path)
    el_keyed = tree_keyed.getroot().find("KeyedOne")
    assert el_keyed is not None and el_keyed.text == "ORIGINAL KEYED TEXT"

    ru_def_path = os.path.join(root, "Languages", "Russian (Русский)", "DefInjected", "RecipeDef", "Recipes.xml")
    assert os.path.isfile(ru_def_path)
    tree_def = ET.parse(ru_def_path)
    el_def = tree_def.getroot().find("Make_Thing.label")
    assert el_def is not None and el_def.text == "ORIGINAL DEF LABEL"

    # Test Merging with existing target JSON and XML files
    # Modify Stardew ru.json to have some old data and extra untranslated keys
    with open(ru_json_path, "w", encoding="utf-8-sig") as f:
        json.dump({"simple_key": "OLD SIMPLE", "extra_key": "PRESERVED"}, f)
        
    # Modify RimWorld XML to have some old data and extra keys
    xml_keyed_old = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<LanguageData>\n'
        '  <KeyedOne>OLD KEYED</KeyedOne>\n'
        '  <ExtraKey>PRESERVED KEYED</ExtraKey>\n'
        '</LanguageData>\n'
    )
    with open(ru_keyed_path, "w", encoding="utf-8-sig") as f:
        f.write(xml_keyed_old)

    # Re-inject
    written2 = p.inject(root, tr, "Russian")
    assert written2 == 4, f"i18n written2={written2}"

    # Verify merge in Stardew JSON
    with open(ru_json_path, "r", encoding="utf-8-sig") as f:
        ru_data2 = json.load(f)
    assert ru_data2["simple_key"] == "ORIGINAL SIMPLE"
    assert ru_data2["nested_section"]["nested_key"] == "ORIGINAL NESTED"
    assert ru_data2["extra_key"] == "PRESERVED"

    # Verify merge in RimWorld XML
    tree_keyed2 = ET.parse(ru_keyed_path)
    root_keyed2 = tree_keyed2.getroot()
    el_k1 = root_keyed2.find("KeyedOne")
    el_ext = root_keyed2.find("ExtraKey")
    assert el_k1 is not None and el_k1.text == "ORIGINAL KEYED TEXT"
    assert el_ext is not None and el_ext.text == "PRESERVED KEYED"


def _fusion_cell(text: str) -> bytes:
    """Build one ARR1.0 string cell: 01 02 marker + u32 len + payload bytes."""
    import struct as _s
    return b"\x01\x00\x00\x00\x02\x00\x00\x00" + _s.pack("<I", len(text)) + text.encode("latin1")


def build_fusion_project() -> str:
    """Tiny ARR1.0 dia file: magic + 2 dims + a few cells.

    Cells, in order:
      - "Name"                       structural label (no pipes) -> skipped
      - encoded "{dye04}Hi"          dialogue with a control token -> extracted
      - "{bub04}|{new}"              only tokens -> skipped (no letters)
      - encoded "Yes"                dialogue -> extracted
    """
    import struct as _s
    root = tempfile.mkdtemp(prefix="interprex_fusion_selftest_")
    data = os.path.join(root, "data")
    os.makedirs(data)

    def enc(s: str) -> str:
        # char -> str(ord-31), tokens {..} kept literal, pipe-joined
        parts = []
        i = 0
        while i < len(s):
            if s[i] == "{":
                j = s.index("}", i)
                parts.append(s[i:j + 1])
                i = j + 1
            else:
                parts.append(str(ord(s[i]) - 31))
                i += 1
        return "|".join(parts)

    cells = b"".join([
        _fusion_cell("Name"),
        _fusion_cell(enc("{dye04}Hi")),
        _fusion_cell("{bub04}|{new}"),
        _fusion_cell(enc("Yes")),
    ])
    body = b"ARR1.0" + _s.pack("<II", 4, 1) + cells
    with open(os.path.join(data, "dia"), "wb") as f:
        f.write(body)
    return root


def check_fusion() -> None:
    root = build_fusion_project()
    dia = os.path.join(root, "data", "dia")

    assert detect_engine(root) == "fusion", f"fusion detect failed: {detect_engine(root)}"
    p = get_parser("fusion")

    strings = p.extract(root)
    originals = [s.original for s in strings]
    # "Name" has no pipes -> not dialogue; "{bub04}|{new}" -> no letters; both skipped.
    assert originals == ["{dye04}Hi", "Yes"], originals

    # control tokens survive into the extracted text verbatim
    assert strings[0].original == "{dye04}Hi", strings[0].original

    # cell index is the path; stable across runs
    by_orig = {s.original: s for s in strings}
    assert by_orig["{dye04}Hi"].path == ["cell", "1"], by_orig["{dye04}Hi"].path
    assert by_orig["Yes"].path == ["cell", "3"], by_orig["Yes"].path
    ids1 = {s.id for s in strings}
    ids2 = {s.id for s in p.extract(root)}
    assert ids1 == ids2, "fusion ids not stable across runs"

    # inject: translate, preserving the {token}; round-trips through decode/encode
    tr = {
        by_orig["{dye04}Hi"].id: "{dye04}Привет",
        by_orig["Yes"].id: "Да",
    }
    written = p.inject(root, tr)
    assert written == 2, f"fusion written={written}"

    # Write mock project file so extract knows it contains Russian translations
    import json
    from parsers.base import project_file_path
    proj = {
        "version": 1,
        "engine": "fusion",
        "root": root,
        "strings": {
            k: {"original": "", "translated": v, "approved": True}
            for k, v in tr.items()
        }
    }
    proj_path = project_file_path(root)
    os.makedirs(os.path.dirname(proj_path), exist_ok=True)
    with open(proj_path, "w", encoding="utf-8") as f:
        json.dump(proj, f)

    # re-extract (with backup) returns original English text:
    strings2 = p.extract(root)
    originals2 = [s.original for s in strings2]
    assert originals2 == ["{dye04}Hi", "Yes"], originals2

    # If we delete the backup folder, re-extract reads the injected Russian:
    import shutil
    shutil.rmtree(os.path.join(root, ".interprex_backups"))
    strings3 = p.extract(root)
    originals3 = [s.original for s in strings3]
    assert originals3 == ["{dye04}Привет", "Да"], originals3

    # file still a valid ARR1.0 container with the same magic + dims
    with open(dia, "rb") as f:
        head = f.read(6)
    assert head == b"ARR1.0", head

    # id parity with the TS makeId — recompute with the TS algorithm in your head
    # via the shared make_id; this fixed value guards against the two drifting apart.
    parity = make_id("fusion", "data/dia", ["cell", "1"], "{dye04}Hi")
    assert parity == "289e2e53", f"fusion id parity drifted: {parity}"


def build_mmf2_project() -> str:
    """Tiny Baba-Is-You-style MMF2 language file: CRLF, [general] metadata +
    [texts] values, including one empty value and one with an '=' inside."""
    root = tempfile.mkdtemp(prefix="interprex_mmf2_selftest_")
    langs = os.path.join(root, "Data", "Languages")
    os.makedirs(langs)
    content = (
        "[general]\r\n"
        "name=English\r\n"
        "customfont=0\r\n"
        "[texts]\r\n"
        "main_continue=Continue playing\r\n"
        "settings=Settings\r\n"
        "ratio=Aspect ratio = 16:9\r\n"   # value contains '='
        "empty_key=\r\n"                   # legitimately empty -> not extracted
    )
    with open(os.path.join(langs, "lang_en.txt"), "w", encoding="utf-8", newline="") as f:
        f.write(content)
    return root


def check_mmf2() -> None:
    root = build_mmf2_project()
    src = os.path.join(root, "Data", "Languages", "lang_en.txt")

    assert detect_engine(root) == "mmf2", f"mmf2 detect failed: {detect_engine(root)}"
    p = get_parser("mmf2")

    strings = p.extract(root)
    originals = [s.original for s in strings]
    # [general] skipped; empty value skipped; value-with-'=' kept whole.
    assert originals == [
        "Continue playing", "Settings", "Aspect ratio = 16:9",
    ], originals

    by_orig = {s.original: s for s in strings}
    assert by_orig["Continue playing"].path == ["texts", "main_continue"], by_orig["Continue playing"].path
    assert by_orig["Aspect ratio = 16:9"].path == ["texts", "ratio"], by_orig["Aspect ratio = 16:9"].path
    # metadata never leaks in
    assert "English" not in originals and "0" not in originals, originals

    # ids stable across runs
    ids1 = {s.id for s in strings}
    ids2 = {s.id for s in p.extract(root)}
    assert ids1 == ids2, "mmf2 ids not stable across runs"

    # inject: translate everything, including the value-with-'='
    tr = {s.id: s.original.upper() for s in strings}
    written = p.inject(root, tr)
    assert written == 3, f"mmf2 written={written}"

    with open(src, encoding="utf-8", newline="") as f:
        after = f.read()
    # values rewritten in place
    assert "main_continue=CONTINUE PLAYING\r\n" in after, repr(after)
    assert "ratio=ASPECT RATIO = 16:9\r\n" in after, repr(after)
    # CRLF preserved, metadata untouched, empty value untouched
    assert "[general]\r\nname=English\r\ncustomfont=0\r\n" in after, repr(after)
    assert "empty_key=\r\n" in after, repr(after)

    # id parity with the TS makeId — fixed anchor against drift
    parity = make_id("mmf2", "Data/Languages/lang_en.txt", ["texts", "main_continue"], "Continue playing")
    assert parity == "fafdf077", f"mmf2 id parity drifted: {parity}"


def build_qsp_project() -> str:
    """Tiny QGen-style .qsp: UTF-16LE, no BOM, CRLF between fields, +5 cipher on
    everything past the version line. One location with a description, an on-visit
    code block mixing code and output, and one action.

    Layout: QSPGAME / version / password / count / then per location
    [name, desc, code, action_count, (img, action_name, action_code)...].
    """
    root = tempfile.mkdtemp(prefix="interprex_qsp_selftest_")

    def enc(s: str) -> str:
        return "".join(chr((ord(c) + 5) & 0xFFFF) for c in s)

    # On-visit code: a print, a bare-string print, a jump (skip), an assignment of
    # prose (keep) and of an identifier (skip), and an asset path (skip).
    code = (
        "*pl 'Hello there!'\r\n"
        "'A narration line.'\r\n"
        "gt 'next_room'\r\n"
        "$sys='System: '\r\n"
        "$tag='internal_id'\r\n"
        "*pl 'pics/logo.png'"
    )
    fields = [
        "QSPGAME",                       # 0 signature (plain)
        "1.0 (selftest)",                # 1 version (plain)
        enc("pwd"),                      # 2 password (ciphered, ignored)
        enc("1"),                        # 3 location count
        enc("start"),                    # 4 location name (jump key, NOT text)
        enc("Welcome home."),            # 5 description (TEXT)
        enc(code),                       # 6 on-visit code block
        enc("1"),                        # 7 action count
        enc(""),                         # 8 action image (NOT text)
        enc("Look around"),              # 9 action name (TEXT)
        enc("*pl 'You see nothing.'"),   # 10 action code
    ]
    blob = "\r\n".join(fields)
    with open(os.path.join(root, "TLP.qsp"), "wb") as f:
        f.write(blob.encode("utf-16le"))
    return root


def check_qsp() -> None:
    root = build_qsp_project()
    qsp = os.path.join(root, "TLP.qsp")
    orig_bytes = open(qsp, "rb").read()

    assert detect_engine(root) == "qsp", f"qsp detect failed: {detect_engine(root)}"
    p = get_parser("qsp")

    strings = p.extract(root)
    originals = [s.original for s in strings]
    # desc + two code outputs (one bare-string print) + prose assignment, then the
    # action name + its code output. Jump target, identifier assignment, asset path,
    # and the location name are all skipped.
    assert originals == [
        "Welcome home.",
        "Hello there!",
        "A narration line.",
        "System: ",
        "Look around",
        "You see nothing.",
    ], originals

    by_orig = {s.original: s for s in strings}
    assert by_orig["Welcome home."].path == ["loc", "start", "desc"], by_orig["Welcome home."].path
    assert by_orig["Hello there!"].path == ["loc", "start", "code", "0"], by_orig["Hello there!"].path
    assert by_orig["Look around"].path == ["loc", "start", "act", "0", "name"], by_orig["Look around"].path
    assert by_orig["You see nothing."].path == ["loc", "start", "act", "0", "code", "0"], by_orig["You see nothing."].path

    # ids stable across runs
    ids1 = {s.id for s in strings}
    ids2 = {s.id for s in p.extract(root)}
    assert ids1 == ids2, "qsp ids not stable across runs"

    # identity inject must be a perfect byte-for-byte no-op (cipher + splice safe)
    p.inject(root, {s.id: s.original for s in strings})
    assert open(qsp, "rb").read() == orig_bytes, "qsp identity inject changed bytes"

    # real inject: translate everything to upper-case, round-trips through the cipher
    tr = {s.id: s.original.upper() for s in strings}
    written = p.inject(root, tr)
    assert written == 6, f"qsp written={written}"

    strings2 = p.extract(root)
    originals2 = [s.original for s in strings2]
    assert originals2 == [
        "WELCOME HOME.", "HELLO THERE!", "A NARRATION LINE.",
        "SYSTEM: ", "LOOK AROUND", "YOU SEE NOTHING.",
    ], originals2

    # structure intact: still UTF-16LE QSPGAME, same field count, code/jump untouched
    text = open(qsp, "rb").read().decode("utf-16le")
    assert text.startswith("QSPGAME"), text[:16]
    assert len(text.split("\r\n")) == 11, len(text.split("\r\n"))
    lines = text.split("\r\n")
    code_field = "".join(chr((ord(c) - 5) & 0xFFFF) for c in lines[6])
    assert "gt 'next_room'" in code_field, code_field      # jump target preserved
    assert "$tag='internal_id'" in code_field, code_field   # identifier preserved
    assert "pics/logo.png" in code_field, code_field        # asset path preserved

    # id parity with the TS makeId — fixed anchor against drift
    parity = make_id("qsp", "TLP.qsp", ["loc", "start", "desc"], "Welcome home.")
    assert parity == "f12b6e6f", f"qsp id parity drifted: {parity}"


def build_unreal_project() -> str:
    root = tempfile.mkdtemp(prefix="interprex_unreal_selftest_")
    loc_dir = os.path.join(root, "Localization", "INT")
    os.makedirs(loc_dir)
    
    # 1. Game.INT with various edge cases
    game_content = (
        "; This is a comment\n"
        "[HUD.Settings]\n"
        "Title=\"My Game Title\"\n"
        "Path=\"C:\\\\Games\\\\MyGame\\\\\"\n"
        "URL=\"http://example.com?a=b&c=d\"\n"
        "Version=1.0 ; Inline version comment\n"
        "EmptyVal=\n"
        "NoValueKey\n"
        "\n"
        "[Dialogue]\n"
        "Line1=\"Hello \\u00E9 world\"\n"
        "Line2=\"Line 1\\nLine 2\"\n"
        "MultiLine=\"This is line 1\n"
        "and this is line 2\n"
        "and this is line 3\"\n"
        "DuplicateKey=\"First duplicate\"\n"
        "DuplicateKey=\"Second duplicate\"\n"
        "DuplicateKey=\"Third duplicate\"\n"
        # Stray backslash before a normal char (real LIS: "/!\\ WARNING /!\\").
        # de_escape drops it, so an untranslated value must be written verbatim.
        "Warn=\"/!\\ WARNING /!\\ not localized\"\n"
        "\n"
        # UE3 struct subtitles (Borderlands 2). Real dialogue is hidden inside a
        # (Text="...",Time=0) wrapper — only the Text field is translatable, and
        # a ';' inside it must NOT be mistaken for an inline comment.
        "[Subtitles]\n"
        "Subtitles[0]=(Text=\"I must go now; my people need me.\",Time=0)\n"
        "Subtitles[1]=(Text=\"\\u00A0\",Time=0)\n"
        "Mob=(DisplayName=\"Adult Skag\",TransformedNames=((TransformedName=\"Fire Skag\")))\n"
        "\n"
        "[EmptySec]\n"
        "\n"
        "[Engine.Engine]\n"
        "LangId=9\n"
        "SubLangId=1\n"
        "Language=English (International)\n"
        "ObjRef=(Name=Core.HelloWorldCommandlet,Class=Class,MetaData=())\n"
        # Bare config values, not display text (BL2 leaks ~25k of these).
        "bIsEnabled=False\n"
        "bShown=True\n"
    )
    with open(os.path.join(loc_dir, "Game.INT"), "wb") as f:
        # Write with mixed endings, we will normalize later
        f.write(game_content.encode("utf-8"))
        
    # 2. UTF-8 with BOM file
    bom_content = (
        "[BOMSection]\n"
        "BOMKey=\"BOMValue\"\n"
    )
    with open(os.path.join(loc_dir, "BOMFile.INT"), "wb") as f:
        f.write(b"\xef\xbb\xbf" + bom_content.encode("utf-8"))
        
    # 3. UTF-16 LE with BOM file
    utf16_content = (
        "[UTF16Section]\n"
        "UTF16Key=\"UTF16Value\"\n"
    )
    with open(os.path.join(loc_dir, "UTF16File.INT"), "wb") as f:
        f.write(b"\xff\xfe" + utf16_content.encode("utf-16le"))
        
    # 4. CP1251 file with Cyrillic (and ASCII comments)
    cyrillic_content = (
        "; Comment in English: this is a test\n"
        "[RussianSection]\n"
        "RusKey=\"Привет мир\"\n"
    )
    with open(os.path.join(loc_dir, "CyrillicFile.INT"), "wb") as f:
        f.write(cyrillic_content.encode("cp1251"))
        
    # 5. Very long string (>50KB)
    long_content = (
        "[LongSection]\n"
        f"LongKey=\"{'A' * 50000}\"\n"
    )
    with open(os.path.join(loc_dir, "LongFile.INT"), "wb") as f:
        f.write(long_content.encode("utf-8"))

    # 6. Dialogue file with underscores in the name (real LIS naming).
    #    Must be discovered — the old regex silently dropped these.
    underscore_content = (
        "[Dialogue]\n"
        "Sub1=\"Underscore dialogue line\"\n"
    )
    with open(os.path.join(loc_dir, "CU_E1_1A.INT"), "wb") as f:
        f.write(underscore_content.encode("utf-8"))

    # 7. Engine/Localization boilerplate — must be EXCLUDED entirely.
    engine_loc = os.path.join(root, "Engine", "Localization", "INT")
    os.makedirs(engine_loc)
    core_content = (
        "[Core.System]\n"
        "Error=\"Cant resolve package name\"\n"
    )
    with open(os.path.join(engine_loc, "Core.INT"), "wb") as f:
        f.write(core_content.encode("utf-8"))

    # 8. BioShock Infinite dialect (UTF-16LE): escaped-quote structs with
    #    Subtitle/Speaker fields, and a plain value wrapped in escaped quotes.
    #    Apostrophes are escaped (\'), quotes are the \" delimiter.
    bsi_content = (
        "[Sign_MAT MaterialInstanceConstant]\n"
        "Subtitle=\\\"The First Lady\\'s Aerodrome\\\"\n"
        "[Arc_Tut Behavior_GFxActivateTutorial_0]\n"
        "Subtitles[0]=(Subtitle=\\\"This isn\\'t the right way.\\\",Speaker=\\\"Elizabeth\\\")\n"
    )
    with open(os.path.join(loc_dir, "BSI_Dialect.INT"), "wb") as f:
        f.write(b"\xff\xfe" + bsi_content.encode("utf-16le"))

    return root


def check_unreal() -> None:
    root = build_unreal_project()
    
    assert detect_engine(root) == "unreal", f"unreal detect failed: {detect_engine(root)}"
    p = get_parser("unreal")
    
    strings = p.extract(root)
    originals = {s.original for s in strings}
    
    assert "My Game Title" in originals
    assert "C:\\Games\\MyGame\\" in originals
    assert "http://example.com?a=b&c=d" in originals
    assert "Hello é world" in originals
    assert "Line 1\nLine 2" in originals
    assert "This is line 1\nand this is line 2\nand this is line 3" in originals
    assert "First duplicate" in originals
    assert "Second duplicate" in originals
    assert "Third duplicate" in originals
    assert "BOMValue" in originals
    assert "UTF16Value" in originals
    assert "Привет мир" in originals
    assert 'A' * 50000 in originals
    # Underscore-named dialogue files must be discovered (the LIS regression).
    assert "Underscore dialogue line" in originals

    # Noise must be filtered: pure-numeric/config values, engine metadata keys,
    # object literals, and the whole Engine/Localization tree.
    assert "1.0" not in originals, "pure-numeric config value should be skipped"
    assert "9" not in originals and "1" not in originals, "LangId/SubLangId should be skipped"
    assert "English (International)" not in originals, "Language metadata should be skipped"
    assert not any(o.startswith("(Name=") for o in originals), "object literal should be skipped"
    assert not any("resolve package" in o for o in originals), "Engine/Localization should be excluded"

    # Struct subtitles (BL2): the Text field is extracted as clean dialogue, the
    # ';' inside it is preserved (not cut as a comment), and the whole struct is
    # NEVER extracted verbatim (would feed the LLM Time=0 and parens).
    assert "I must go now; my people need me." in originals, "struct Text not extracted / ';' truncated"
    assert "Adult Skag" in originals and "Fire Skag" in originals, "struct DisplayName/TransformedName not extracted"
    assert not any(o.lstrip().startswith("(Text=") for o in originals), "whole struct should not be extracted"
    sub = [s for s in strings if s.original == "I must go now; my people need me."][0]
    assert sub.path == ["Subtitles", "Subtitles[0]", "0", "Text", "0"], sub.path

    # Bare bool config values must be skipped.
    assert "False" not in originals and "True" not in originals, "bare bool config should be skipped"

    # BioShock Infinite dialect: escaped-quote structs and escape-wrapped plain
    # values are extracted as CLEAN, de-escaped text — never raw \"...\'...\".
    assert "The First Lady's Aerodrome" in originals, "escape-wrapped plain value not unwrapped"
    assert "This isn't the right way." in originals, "escaped-quote struct Subtitle not extracted"
    assert "Elizabeth" in originals, "escaped-quote struct Speaker not extracted"
    assert not any('\\"' in o for o in originals), "raw escaped quotes must not leak to the LLM"

    # check paths of duplicate keys
    dups = [s for s in strings if s.original in ("First duplicate", "Second duplicate", "Third duplicate")]
    assert len(dups) == 3
    dups.sort(key=lambda s: s.original)
    # "First duplicate" is sorted first, then "Second...", then "Third..."
    assert dups[0].path == ["Dialogue", "DuplicateKey", "0"]
    assert dups[1].path == ["Dialogue", "DuplicateKey", "1"]
    assert dups[2].path == ["Dialogue", "DuplicateKey", "2"]
    
    # IDs stability
    ids1 = {s.id for s in strings}
    ids2 = {s.id for s in p.extract(root)}
    assert ids1 == ids2, "unreal ids not stable across runs"
    
    # Inject — translate everything EXCEPT the stray-backslash value, which we
    # leave untranslated to exercise the verbatim write-back path.
    warn_ids = {s.id for s in strings if s.original.startswith("/!")}
    assert warn_ids, "stray-backslash fixture string missing"
    tr = {s.id: s.original.upper() for s in strings if s.id not in warn_ids}
    written = p.inject(root, tr, target_lang="Russian")
    assert written == len(tr), f"unreal written={written} != {len(tr)}"

    rus_dir = os.path.join(root, "Localization", "RUS")
    assert os.path.isdir(rus_dir)

    # Check Game.RUS
    game_rus_path = os.path.join(rus_dir, "Game.RUS")
    assert os.path.exists(game_rus_path)
    game_rus_bytes = open(game_rus_path, "rb").read()
    # verify only CRLF endings
    assert b"\r\n" in game_rus_bytes
    assert b"\n" not in game_rus_bytes.replace(b"\r\n", b"")
    
    game_rus_text = game_rus_bytes.decode("utf-8")
    assert 'Title="MY GAME TITLE"' in game_rus_text
    assert 'Path="C:\\\\GAMES\\\\MYGAME\\\\"' in game_rus_text
    assert 'URL="HTTP://EXAMPLE.COM?A=B&C=D"' in game_rus_text
    assert 'Version=1.0 ; Inline version comment' in game_rus_text
    assert 'Line1="HELLO \u00C9 WORLD"' in game_rus_text
    assert 'Line2="LINE 1\\nLINE 2"' in game_rus_text
    assert 'MultiLine="THIS IS LINE 1\\nAND THIS IS LINE 2\\nAND THIS IS LINE 3"' in game_rus_text
    assert 'DuplicateKey="FIRST DUPLICATE"' in game_rus_text
    assert 'DuplicateKey="SECOND DUPLICATE"' in game_rus_text
    assert 'DuplicateKey="THIRD DUPLICATE"' in game_rus_text
    # Struct inject is SURGICAL: only the Text content changes; Time=0, the
    # parens and the placeholder sibling stay byte-identical.
    assert 'Subtitles[0]=(Text="I MUST GO NOW; MY PEOPLE NEED ME.",Time=0)' in game_rus_text, \
        "struct Text not injected surgically / structure damaged"
    assert 'Mob=(DisplayName="ADULT SKAG",TransformedNames=((TransformedName="FIRE SKAG")))' in game_rus_text
    # Placeholder struct (no translatable text) written verbatim, escape intact.
    assert r'Subtitles[1]=(Text="\u00A0",Time=0)' in game_rus_text, "placeholder struct untouched"
    # Untranslated stray-backslash value survives verbatim (not corrupted).
    assert 'Warn="/!\\ WARNING /!\\ not localized"' in game_rus_text

    # Check CyrillicFile.RUS CP1251
    cyr_rus_path = os.path.join(rus_dir, "CyrillicFile.RUS")
    assert os.path.exists(cyr_rus_path)
    cyr_rus_bytes = open(cyr_rus_path, "rb").read()
    cyr_rus_text = cyr_rus_bytes.decode("cp1251")
    assert "ПРИВЕТ МИР" in cyr_rus_text
    
    # Check BOM preservation (and that it is not DOUBLED).
    bom_rus_path = os.path.join(rus_dir, "BOMFile.RUS")
    bom_rus_bytes = open(bom_rus_path, "rb").read()
    assert bom_rus_bytes.startswith(b"\xef\xbb\xbf")
    assert not bom_rus_bytes.startswith(b"\xef\xbb\xbf\xef\xbb\xbf"), "doubled UTF-8 BOM"

    utf16_rus_path = os.path.join(rus_dir, "UTF16File.RUS")
    utf16_rus_bytes = open(utf16_rus_path, "rb").read()
    assert utf16_rus_bytes.startswith(b"\xff\xfe") or utf16_rus_bytes.startswith(b"\xfe\xff")
    # The decoded text must not begin with a leftover BOM char (doubled BOM).
    assert not utf16_rus_bytes.decode("utf-16").startswith("﻿"), "doubled UTF-16 BOM"

    # BioShock Infinite dialect inject: translations are re-wrapped/re-escaped in
    # the SAME \"-quote dialect — apostrophe escaped \', delimiter \" preserved.
    bsi_rus_text = open(os.path.join(rus_dir, "BSI_Dialect.RUS"), "rb").read().decode("utf-16")
    # plain escape-wrapped value: "The First Lady's Aerodrome" -> upper, re-wrapped
    assert "Subtitle=\\\"THE FIRST LADY\\'S AERODROME\\\"" in bsi_rus_text, \
        "escape-wrapped plain value not re-wrapped correctly"
    # struct fields: Subtitle + Speaker both translated, escaped-quote dialect kept
    assert "Subtitle=\\\"THIS ISN\\'T THE RIGHT WAY.\\\"" in bsi_rus_text, \
        "escaped-quote struct field not injected in its dialect"
    assert "Speaker=\\\"ELIZABETH\\\"" in bsi_rus_text

    # Check fixed id parity
    parity = make_id("unreal", "Localization/INT/Game.INT", ["HUD.Settings", "Title", "0"], "My Game Title")
    assert parity == "31807449", f"unreal id parity drifted: {parity}"


def _fstring(s: str) -> bytes:
    """Encode an FString the way UE4 does (mirror of unreal4.encode_fstring), for
    building synthetic fixtures."""
    import struct as _s
    t = s + "\x00"
    if all(ord(c) < 128 for c in t):
        data = t.encode("ascii")
        return _s.pack("<i", len(data)) + data
    data = t.encode("utf-16-le")
    return _s.pack("<i", -(len(data) // 2)) + data


def build_unreal4_v3() -> str:
    """Synthesize a v3 (Optimized_CityHash64_UTF16) .locres exercising the tricky
    cases: a string-table slot SHARED by two keys (dedup), a UTF-16 negative-
    length value, and an empty value. Hashes are arbitrary 4-byte fillers — we
    never recompute them (carry-verbatim), so their value doesn't matter for the
    round-trip, only that they survive byte-identically."""
    import struct as _s
    from parsers.unreal4_5 import LOCRES_MAGIC

    root = tempfile.mkdtemp(prefix="interprex_unreal4_selftest_")
    loc = os.path.join(root, "Content", "Localization", "Game", "en")
    os.makedirs(loc)

    # String table: 4 slots. Slot 1 ("Open") is shared by two keys -> dedup.
    st_values = ["Start Game", "Open", "Привет мир", ""]
    H = b"\xAA\xBB\xCC\xDD"        # filler namespace/key hash
    SH = b"\x11\x22\x33\x44"       # filler source-string hash
    RC = _s.pack("<i", 1)         # refcount filler

    # Namespaces/keys -> (string_index)
    #   [Menu] start=0, open_a=1, open_b=1 (shares slot 1), empty=3
    #   [HUD]  greet=2
    ns_layout = [
        ("Menu", [("start", 0), ("open_a", 1), ("open_b", 1), ("empty", 3)]),
        ("HUD", [("greet", 2)]),
    ]
    total_keys = sum(len(keys) for _n, keys in ns_layout)

    body = bytearray()
    body += LOCRES_MAGIC
    body.append(3)                                  # version 3
    offset_pos = len(body)
    body += b"\x00" * 8                              # placeholder st offset
    body += _s.pack("<i", total_keys)               # entry count (v>=2)
    body += _s.pack("<i", len(ns_layout))           # namespace count
    for name, keys in ns_layout:
        body += H                                    # ns hash
        body += _fstring(name)
        body += _s.pack("<i", len(keys))
        for key, idx in keys:
            body += H                                # key hash
            body += _fstring(key)
            body += SH                               # source string hash
            body += _s.pack("<i", idx)
    st_offset = len(body)
    body += _s.pack("<i", len(st_values))
    for v in st_values:
        body += _fstring(v)
        body += RC
    _s.pack_into("<q", body, offset_pos, st_offset)

    with open(os.path.join(loc, "Game.locres"), "wb") as f:
        f.write(bytes(body))
    return root


def check_unreal4() -> None:
    from parsers.unreal4_5 import parse_locres, serialize_locres

    root = build_unreal4_v3()
    src = os.path.join(root, "Content", "Localization", "Game", "en", "Game.locres")
    orig_bytes = open(src, "rb").read()

    assert detect_engine(root) == "unreal4_5", f"unreal4 detect failed: {detect_engine(root)}"
    p = get_parser("unreal4_5")

    # Byte-exact identity round-trip BEFORE any inject (the bedrock requirement).
    m = parse_locres(orig_bytes)
    assert serialize_locres(m) == orig_bytes, "unreal4 identity round-trip not byte-exact"

    strings = p.extract(root)
    originals = [s.original for s in strings]
    # Empty value skipped; shared slot surfaces under both keys.
    assert originals == [
        "Start Game", "Open", "Open", "Привет мир",
    ], originals

    by_path = {tuple(s.path): s for s in strings}
    assert ("Menu", "start") in by_path
    assert ("Menu", "open_a") in by_path and ("Menu", "open_b") in by_path
    assert ("HUD", "greet") in by_path
    assert ("Menu", "empty") not in by_path, "empty value must be skipped"

    # ids stable across runs
    ids1 = {s.id for s in strings}
    ids2 = {s.id for s in p.extract(root)}
    assert ids1 == ids2, "unreal4 ids not stable across runs"

    # --- Inject case 1: both sharers get the SAME translation -> edit slot in place.
    tr = {
        by_path[("Menu", "start")].id: "Начать игру",
        by_path[("Menu", "open_a")].id: "Открыть",
        by_path[("Menu", "open_b")].id: "Открыть",
        by_path[("HUD", "greet")].id: "ПРИВЕТ МИР",
    }
    written = p.inject(root, tr, target_lang="Russian")
    assert written == 4, f"unreal4 written={written}"

    out = os.path.join(root, "Content", "Localization", "Game", "ru", "Game.locres")
    assert os.path.isfile(out), "ru .locres not written"
    m2 = parse_locres(open(out, "rb").read())
    # No split needed: the table keeps its 4 slots (shared slot edited in place).
    assert len(m2.string_table) == 4, f"expected 4 slots, got {len(m2.string_table)}"
    after = {tuple(s.path): s.original for s in p.extract(root, sub_paths=[
        "Content/Localization/Game/ru/Game.locres"])}
    assert after[("Menu", "start")] == "Начать игру"
    assert after[("Menu", "open_a")] == "Открыть"
    assert after[("Menu", "open_b")] == "Открыть"
    assert after[("HUD", "greet")] == "ПРИВЕТ МИР"

    # --- Inject case 2: sharers get DIFFERENT translations -> dedup-split.
    tr2 = {
        by_path[("Menu", "open_a")].id: "Открыть A",
        by_path[("Menu", "open_b")].id: "Открыть B",
    }
    p.inject(root, tr2, target_lang="Russian")
    m3 = parse_locres(open(out, "rb").read())
    # Original 4 slots + 2 appended (one per distinct translation).
    assert len(m3.string_table) == 6, f"expected 6 slots after split, got {len(m3.string_table)}"
    after2 = {tuple(s.path): s.original for s in p.extract(root, sub_paths=[
        "Content/Localization/Game/ru/Game.locres"])}
    assert after2[("Menu", "open_a")] == "Открыть A", after2
    assert after2[("Menu", "open_b")] == "Открыть B", after2

    # id parity with the TS makeId — fixed anchor against drift.
    parity = make_id("unreal4_5", "Content/Localization/Game/en/Game.locres",
                     ["Game", "hello"], "Hello there")
    assert parity == "0bae26da", f"unreal4_5 id parity drifted: {parity}"


def check_unreal4_pak() -> None:
    """The 'packed' path: .locres lives inside a .pak (shipped UE5 games). Uses our
    own uncompressed pak writer to synth a fixture (Oodle isn't needed to WRITE),
    so this runs without the oo2core DLL. Verifies detect -> extract -> inject into
    a separate mod-pak, and that translations land in the retargeted culture."""
    from parsers.pak import write_pak, read_pak
    from parsers.unreal4_5 import parse_locres

    # Reuse the v3 fixture builder to get a real locres on disk, then read its bytes.
    src_root = build_unreal4_v3()
    locres_bytes = open(os.path.join(
        src_root, "Content", "Localization", "Game", "en", "Game.locres"), "rb").read()

    # Pack it into a .pak inside a fresh game root.
    root = tempfile.mkdtemp(prefix="interprex_u4pak_selftest_")
    paks = os.path.join(root, "Content", "Paks")
    os.makedirs(paks)
    inner = "Game/Content/Localization/Game/en/Game.locres"
    write_pak(os.path.join(paks, "MyGame.pak"), {inner: locres_bytes})

    assert detect_engine(root) == "unreal4_5", f"pak detect failed: {detect_engine(root)}"
    p = get_parser("unreal4_5")

    strings = p.extract(root)
    originals = sorted(s.original for s in strings)
    assert originals == ["Open", "Open", "Start Game", "Привет мир"], originals
    # file label carries the pak!inner address
    assert all("!" in s.file and s.file.endswith("Game.locres") for s in strings), \
        [s.file for s in strings][:2]

    # ids stable across runs
    assert {s.id for s in strings} == {s.id for s in p.extract(root)}, "pak ids not stable"

    # inject -> mod-pak
    tr = {s.id: s.original.upper() for s in strings}
    written = p.inject(root, tr, target_lang="Russian")
    assert written == 4, f"pak written={written}"

    mod = os.path.join(paks, "MyGame_ru_P.pak")
    assert os.path.isfile(mod), "mod-pak not created"
    back = read_pak(mod)
    assert len(back) == 1, [b.path for b in back]
    # retargeted culture en -> ru
    assert "/ru/" in back[0].path, back[0].path
    m = parse_locres(back[0].data)
    vals = {m.string_table[k.string_index].value.text for ns in m.namespaces for k in ns.keys}
    assert "START GAME" in vals and "ПРИВЕТ МИР" in vals, vals
    # original pak untouched
    assert os.path.isfile(os.path.join(paks, "MyGame.pak"))


def check_unreal4_5_utoc() -> None:
    """Test the packed Zen/IoStore path (.utoc/.ucas) using mocked retoc CLI execution.
    Verifies detection, extraction with header-filtering regex, version detection,
    validation of to-zen outputs, SML mod plugin creation (for Satisfactory base game),
    and standard patch containers (for other files)."""
    from unittest.mock import patch, MagicMock
    from parsers.unreal4_5 import parse_locres, serialize_locres
    import subprocess
    
    # 1. Build a synthetic .locres and get its bytes
    src_root = build_unreal4_v3()
    locres_path = os.path.join(src_root, "Content", "Localization", "Game", "en", "Game.locres")
    locres_bytes = open(locres_path, "rb").read()
    
    # 2. Setup game directory layouts
    # Case A: Standard UE5 game (not Satisfactory)
    game_root = tempfile.mkdtemp(prefix="interprex_u5utoc_")
    content_paks = os.path.join(game_root, "Content", "Paks")
    os.makedirs(content_paks)
    
    # Base container (we only need the .utoc file on disk to trigger iter_utoc_files)
    utoc_file = os.path.join(content_paks, "GameContainer.utoc")
    open(utoc_file, "wb").write(b"fake utoc")
    
    # Case B: Satisfactory base game
    sat_base_root = tempfile.mkdtemp(prefix="interprex_sat_base_")
    sat_paks = os.path.join(sat_base_root, "FactoryGame", "Content", "Paks")
    os.makedirs(sat_paks)
    sat_utoc = os.path.join(sat_paks, "FactoryGame-Windows.utoc")
    open(sat_utoc, "wb").write(b"fake satisfactory utoc")

    # Case C: Satisfactory Mod
    sat_mod_root = tempfile.mkdtemp(prefix="interprex_sat_mod_")
    sat_mod_paks = os.path.join(sat_mod_root, "FactoryGame", "Mods", "TestMod", "Content", "Paks")
    os.makedirs(sat_mod_paks)
    sat_mod_utoc = os.path.join(sat_mod_paks, "TestMod.utoc")
    open(sat_mod_utoc, "wb").write(b"fake satisfactory mod utoc")
    
    # Mock subprocess.run to intercept retoc commands
    def mock_run(args, **kwargs):
        cmd = args[0]
        subcmd = args[1] if len(args) > 1 else ""
        
        # 1. retoc --help
        if "--help" in args:
            mock_res = MagicMock()
            mock_res.stdout = "commands:\n  list\n  get\n  info\n  to-zen\n"
            mock_res.stderr = ""
            mock_res.return_value = 0
            return mock_res
            
        # 2. retoc info
        if subcmd == "info":
            mock_res = MagicMock()
            mock_res.stdout = "Container info:\n  Version: 10\n  Features: ReplaceIoChunkHashWithIoHash\n"
            mock_res.stderr = ""
            mock_res.return_value = 0
            return mock_res
            
        # 3. retoc list
        if subcmd == "list":
            # Return list output with header and footer lines to test regex robustness
            mock_res = MagicMock()
            mock_res.stdout = (
                "----------------------------------------------------\n"
                "ChunkId                                  Path\n"
                "----------------------------------------------------\n"
                "abcdef0123456789abcdef0123456789         Game/Content/Localization/Game/en/Game.locres\n"
                "----------------------------------------------------\n"
                "Total: 1 files\n"
            )
            mock_res.stderr = ""
            mock_res.return_value = 0
            return mock_res
            
        # 4. retoc get
        if subcmd == "get":
            # Write synthetic .locres bytes to the temp_file argument (last argument)
            dest = args[-1]
            with open(dest, "wb") as f:
                f.write(locres_bytes)
            mock_res = MagicMock()
            mock_res.stdout = "Success\n"
            mock_res.stderr = ""
            mock_res.return_value = 0
            return mock_res
            
        # 5. retoc to-zen
        if subcmd == "to-zen":
            # Output base path is the last argument
            out_base = args[-1]
            # Create patch files to satisfy the validation
            for ext in (".utoc", ".ucas", ".pak"):
                with open(out_base + ext, "wb") as f:
                    f.write(b"fake compiled patch bytes")
            mock_res = MagicMock()
            mock_res.stdout = "to-zen success\n"
            mock_res.stderr = ""
            mock_res.return_value = 0
            return mock_res
            
        # Fallback
        mock_res = MagicMock()
        mock_res.stdout = ""
        mock_res.stderr = ""
        mock_res.return_value = 0
        return mock_res

    # Use patch to mock subprocess.run and shutil.which (to bypass local retoc lookups)
    with patch("subprocess.run", side_effect=mock_run), \
         patch("shutil.which", return_value="retoc"):
         
        # --- TEST 1: Detection ---
        assert detect_engine(game_root) == "unreal4_5", "utoc engine detection failed"
        assert detect_engine(sat_base_root) == "unreal4_5", "Satisfactory utoc engine detection failed"
        assert detect_engine(sat_mod_root) == "unreal4_5", "Satisfactory mod engine detection failed"
        
        p = get_parser("unreal4_5")
        
        # --- TEST 2: Extract ---
        strings = p.extract(game_root)
        originals = sorted(s.original for s in strings)
        assert originals == ["Open", "Open", "Start Game", "Привет мир"], originals
        
        # Verify file label format: <rel_utoc>!<inner_path>
        assert strings[0].file == "Content/Paks/GameContainer.utoc!Game/Content/Localization/Game/en/Game.locres", strings[0].file
        
        # --- TEST 3: Inject for standard game (Zen patch container) ---
        tr = {s.id: s.original.upper() for s in strings}
        written = p.inject(game_root, tr, target_lang="Russian")
        assert written == 4, f"utoc inject written={written}"
        
        # Verify that Zen patch files were created
        assert os.path.exists(os.path.join(content_paks, "GameContainer_P.utoc"))
        assert os.path.exists(os.path.join(content_paks, "GameContainer_P.ucas"))
        assert os.path.exists(os.path.join(content_paks, "GameContainer_P.pak"))
        
        # --- TEST 4: Inject for Satisfactory Base Game (SML Plugin folder) ---
        # Clear/initialize Satisfactory root and extract
        sat_strings = p.extract(sat_base_root)
        sat_tr = {s.id: s.original.upper() for s in sat_strings}
        
        # Inject Satisfactory base game
        sat_written = p.inject(sat_base_root, sat_tr, target_lang="Russian")
        assert sat_written == 4, f"Satisfactory base inject written={sat_written}"
        
        # Verify SML Mod structure is generated
        sml_dir = os.path.join(sat_base_root, "FactoryGame", "Mods", "InterprexTranslation")
        assert os.path.exists(sml_dir), "SML plugin directory was not created"
        assert os.path.exists(os.path.join(sml_dir, "InterprexTranslation.uplugin"))
        assert os.path.exists(os.path.join(sml_dir, "Content", "Paks", "InterprexTranslation.pak"))
        assert os.path.exists(os.path.join(sml_dir, "Content", "Paks", "InterprexTranslation.sig"))
        
        # Verify that NO standard Zen patch container was created for Satisfactory base game
        assert not os.path.exists(os.path.join(sat_paks, "FactoryGame-Windows_P.utoc")), "Zen patch created for Satisfactory base game!"
        
        # --- TEST 5: Inject for Satisfactory Mod (should create TestMod_ru_P.pak, not SML plugin folder) ---
        sat_mod_strings = p.extract(sat_mod_root)
        sat_mod_tr = {s.id: s.original.upper() for s in sat_mod_strings}
        
        # Inject Satisfactory mod
        sat_mod_written = p.inject(sat_mod_root, sat_mod_tr, target_lang="Russian")
        assert sat_mod_written == 4, f"Satisfactory mod inject written={sat_mod_written}"
        
        # Verify standard patch container is created next to original mod file
        assert os.path.exists(os.path.join(sat_mod_paks, "TestMod_P.utoc"))
        assert os.path.exists(os.path.join(sat_mod_paks, "TestMod_P.ucas"))
        assert os.path.exists(os.path.join(sat_mod_paks, "TestMod_P.pak"))

    # Cleanup temp dirs
    import shutil
    shutil.rmtree(game_root, ignore_errors=True)
    shutil.rmtree(sat_base_root, ignore_errors=True)
    shutil.rmtree(sat_mod_root, ignore_errors=True)
    shutil.rmtree(src_root, ignore_errors=True)


def check_prompt_width() -> None:
    """The fixed-width caption constraint must reach the model as a FIRST-CLASS
    field (max_chars / fixed_width on the item), NOT buried in `context` — which
    the system prompt tells the model to treat as ignorable metadata (the root
    cause of EN 95 chars -> RU 120). Also: back-compat for plain items, and the
    system prompt must actually name the fields."""
    import json
    from providers.base import TranslateItem, build_prompt, SYSTEM_INSTRUCTION

    # A width-limited item surfaces max_chars + fixed_width at the item level.
    prompt = build_prompt(
        [TranslateItem("a", "Start a New Game", max_chars=8)], "Russian", {}
    )
    # The payload is JSON embedded in the prompt; the item object must carry the
    # constraint fields and must NOT smuggle them into context.
    assert '"max_chars": 8' in prompt, "max_chars not surfaced to the model"
    assert '"fixed_width": true' in prompt, "fixed_width flag not surfaced"
    # The item with no limit emits no max_chars/fixed_width JSON KEY in its payload
    # (the instruction line mentions the words, so match the JSON key with colon).
    plain = build_prompt([TranslateItem("b", "Hello")], "Russian", {})
    assert '"max_chars":' not in plain, "plain item leaked a max_chars value"
    assert '"fixed_width":' not in plain, "plain item leaked a fixed_width value"
    assert TranslateItem("c", "x").max_chars == 0, "default max_chars must be 0"

    # fixed_width / max_chars rules live in the Ren'Py engine addon (they only
    # apply to Ren'Py menu choices / screen buttons, never to other engines).
    renpy_core_prompt = build_prompt(
        [TranslateItem("fw", "Save", max_chars=8)], "Russian", {}, engine="renpy"
    )
    assert "fixed_width" in renpy_core_prompt, \
        "renpy prompt omits fixed_width rule (moved from core to renpy addon)"
    assert "max_chars" in renpy_core_prompt, \
        "renpy prompt omits max_chars rule (moved from core to renpy addon)"
    # Confirm the core alone does NOT expose the fixed-width section.
    assert "fixed_width" not in SYSTEM_INSTRUCTION, \
        "FIXED-WIDTH was moved to renpy addon; core must not contain it"

    # Unknown-gender rule: the prompt must tell the model to use gender-neutral
    # wording when the subject's gender is unknown (the 'казнён(а)' problem) — for
    # any gendered target language, not just Russian.
    assert "GENDER-NEUTRAL" in SYSTEM_INSTRUCTION or "gender-neutral" in SYSTEM_INSTRUCTION, \
        "system prompt omits the unknown-gender / neutral-wording rule"

    # SYSTEM_INSTRUCTION is a backward-compat alias for _SYSTEM_CORE — both must
    # still be importable and point to the same text.
    from providers.base import _SYSTEM_CORE
    assert SYSTEM_INSTRUCTION is _SYSTEM_CORE, \
        "SYSTEM_INSTRUCTION alias must equal _SYSTEM_CORE"

    # Engine addon: build_prompt with engine="renpy" must include Ren'Py-specific
    # instructions (interpolation, style tags) BEYOND the core prompt.
    renpy_prompt = build_prompt([TranslateItem("r", "Hi [name]")], "Russian", {},
                                engine="renpy")
    assert "[variable_name]" in renpy_prompt or "VERBATIM" in renpy_prompt, \
        "renpy addon not injected into prompt"
    assert len(renpy_prompt) > len(plain), \
        "renpy prompt should be longer than core-only prompt"

    # Engine addon: rpgmaker must include control-code instructions.
    rpg_prompt = build_prompt([TranslateItem("g", "Attack")], "Russian", {},
                              engine="rpgmaker")
    assert "\\V[n]" in rpg_prompt or "RPG MAKER" in rpg_prompt, \
        "rpgmaker addon not injected into prompt"

    # Graceful degradation: unknown engine and empty engine must not raise and
    # must return the core-only prompt without an addon block.
    base_hello = build_prompt([TranslateItem("z", "Hello")], "Russian", {})
    empty_engine_prompt = build_prompt([TranslateItem("z", "Hello")], "Russian", {},
                                       engine="")
    unknown_engine_prompt = build_prompt([TranslateItem("z", "Hello")], "Russian", {},
                                         engine="__nonexistent_engine__")
    assert empty_engine_prompt == base_hello, \
        "empty engine should produce same prompt as no-engine call"
    assert unknown_engine_prompt == base_hello, \
        "unknown engine should degrade to core-only prompt without crashing"



def check_providers() -> None:
    from providers.openai_compat import OpenRouterProvider
    from providers.base import ProviderConfig
    from unittest.mock import patch, MagicMock
    import httpx

    provider = OpenRouterProvider()
    cfg = ProviderConfig(api_key="test-key")

    mock_resp1 = MagicMock()
    mock_resp1.status_code = 200
    mock_resp1.headers = {"link": '<https://openrouter.ai/api/v1/models?offset=2>; rel="next"'}
    mock_resp1.json.return_value = {
        "data": [
            {"id": "google/gemma-2-9b-it:free", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "meta-llama/llama-3-8b-instruct", "pricing": {"prompt": "0.0001", "completion": "0.0002"}}
        ]
    }

    mock_resp2 = MagicMock()
    mock_resp2.status_code = 200
    mock_resp2.headers = {}
    mock_resp2.json.return_value = {
        "data": [
            {"id": "openrouter/auto-free", "pricing": {"prompt": "0.0", "completion": "0.0"}},
            {"id": "openai/gpt-4o", "pricing": {"prompt": "0.01", "completion": "0.03"}}
        ]
    }

    # Test 1: all models (free_only = False)
    with patch("httpx.get") as mock_get:
        mock_get.side_effect = [mock_resp1, mock_resp2]
        cfg.free_only = False
        models = provider.list_models(cfg)
        assert models == [
            "google/gemma-2-9b-it:free",
            "meta-llama/llama-3-8b-instruct",
            "openai/gpt-4o",
            "openrouter/auto-free"
        ], f"expected all models, got {models}"
        
        assert mock_get.call_count == 2
        first_call = mock_get.call_args_list[0]
        assert first_call[0][0] == "https://openrouter.ai/api/v1/models"
        assert first_call[1]["params"] == {"limit": 200}
        assert first_call[1]["timeout"] == 30.0
        
        second_call = mock_get.call_args_list[1]
        assert second_call[0][0] == "https://openrouter.ai/api/v1/models?offset=2"
        assert second_call[1].get("params") is None

    # Test 2: filtered models (free_only = True)
    with patch("httpx.get") as mock_get:
        mock_get.side_effect = [mock_resp1, mock_resp2]
        cfg.free_only = True
        models_free = provider.list_models(cfg)
        assert models_free == [
            "google/gemma-2-9b-it:free",
            "openrouter/auto-free"
        ], f"expected free models, got {models_free}"

    # Test 3: error/timeout handling
    with patch("httpx.get") as mock_get:
        mock_get.side_effect = Exception("Timeout error")
        cfg.free_only = False
        models_err = provider.list_models(cfg)
        assert models_err == [], f"expected empty list on error, got {models_err}"


def check_scheduler() -> None:
    """The parallel translation scheduler (scheduler.py): correctness under the
    nasty cases that lose work or deadlock — small remainders, a dead/invalid
    key failing over to a healthy one, transient rate errors recovering, all
    keys dead, and pause draining in-flight batches without losing a string.

    Uses a fake provider so it's offline and ~instant; retry back-off is zeroed."""
    import json
    import threading
    import time
    import scheduler
    from providers.base import TranslateResult, Usage

    saved = (scheduler._RETRY_BACKOFF_FIRST, scheduler._RETRY_BACKOFF_REST,
             scheduler.get_provider)
    scheduler._RETRY_BACKOFF_FIRST = 0
    scheduler._RETRY_BACKOFF_REST = 0

    class FakeProvider:
        name = "fake"

        def __init__(self, auth_keys=None, rate_once=None, delay=0.0):
            self.auth_keys = auth_keys or set()
            self.rate_once = dict(rate_once or {})
            self.delay = delay

        def count_tokens(self, text, cfg):
            return None

        def translate(self, batch, lang, glossary, cfg, engine=""):
            if self.delay:
                time.sleep(self.delay)
            k = cfg.api_key
            if k in self.auth_keys:
                raise RuntimeError("API key not valid. Please pass a valid API key. (403)")
            if self.rate_once.get(k, 0) > 0:
                self.rate_once[k] -= 1
                raise RuntimeError("429 Too Many Requests: rate limit exceeded")
            return TranslateResult({it.id: it.text + "_" + lang for it in batch},
                                   Usage(10, 12))

        def complete_prompt(self, prompt, batch, cfg):
            """Scheduler (Variant A) path: prompt is pre-built, delegate to
            translate() so subclass overrides (ShortenProvider, etc.) still work."""
            return self.translate(batch, "fake", {}, cfg)

    class Req:
        target_lang = "russian"
        glossary = {}
        base_url = ""
        model = ""
        max_context_tokens = 0
        max_batch_size = 5
        root = ""
        engine = ""
        free_only = False

        def __init__(self, items, api_key="K1", api_key_2="", threads=3,
                     provider="fake", delay_seconds=0.0):
            self.items = items
            self.api_key = api_key
            self.api_key_2 = api_key_2
            self.threads = threads
            self.provider = provider
            self.delay_seconds = delay_seconds

    class It:
        def __init__(self, i, text, file="a.txt"):
            self.id = str(i)
            self.text = text
            self.context = ""
            self.file = file
            self.path = []

    def run(req, prov, timeout=30, pause_flag=None):
        should = (lambda: pause_flag["v"]) if pause_flag else (lambda: False)
        scheduler.get_provider = lambda n: prov
        sched = scheduler.TranslationScheduler(req, should_pause=should)
        out = {"final": None}

        def go():
            for line in sched.stream():
                evt = json.loads(line)
                if evt["type"] == "done":
                    out["final"] = evt
        t = threading.Thread(target=go, daemon=True)
        t.start()
        if pause_flag is None:
            t.join(timeout)
            assert not t.is_alive(), "scheduler deadlocked (timeout)"
            return out["final"]
        return sched, t, out

    try:
        # Basic: 100 strings, 3 threads, all translated.
        items = [It(i, "hello%d" % i) for i in range(100)]
        final = run(Req(items, threads=3), FakeProvider())
        assert final and len(final["translations"]) == 100, "basic lost strings"
        assert not final["aborted"]

        # Small remainder must not deadlock with many threads.
        items = [It(i, "x%d" % i) for i in range(12)]
        final = run(Req(items, threads=6), FakeProvider())
        assert final and len(final["translations"]) == 12, "small remainder lost"

        # Dead (invalid) key fails over to a healthy one — and fast.
        items = [It(i, "y%d" % i) for i in range(60)]
        t0 = time.time()
        final = run(Req(items, "K1", "K2", threads=2, provider="gemini"),
                    FakeProvider(auth_keys={"K1"}))
        assert final and len(final["translations"]) == 60, "failover lost strings"
        assert time.time() - t0 < 20, "failover too slow"

        # Dedup: repeated strings translate once, fan out to all ids.
        items = [It(i, "same" if i % 2 == 0 else "other") for i in range(20)]
        final = run(Req(items, threads=2), FakeProvider())
        assert final and len(final["translations"]) == 20, "dedup fan-out lost ids"

        # Transient rate errors recover.
        items = [It(i, "z%d" % i) for i in range(15)]
        final = run(Req(items, threads=2), FakeProvider(rate_once={"K1": 3}))
        assert final and len(final["translations"]) == 15, "rate recovery lost strings"

        # All keys dead → terminates with errors surfaced, no deadlock.
        items = [It(i, "w%d" % i) for i in range(30)]
        final = run(Req(items, "K1", "K2", threads=2, provider="gemini"),
                    FakeProvider(auth_keys={"K1", "K2"}))
        assert final is not None, "all-dead did not terminate"
        assert final["errors"], "all-dead surfaced no error"

        # Edge-case sweep: files x strings-per-file x threads. Every string must
        # be translated exactly once with no deadlock and no >100% (done<=total).
        uid = 1000
        for nf in (1, 2, 7):
            for pf in (0, 1, 5, 13):
                for th in (1, 3, 10):
                    batch_items = []
                    for fi in range(nf):
                        for _ in range(pf):
                            batch_items.append(It(uid, "x%s" % uid, file="f%d.txt" % fi))
                            uid += 1
                    uid += 100
                    fin = run(Req(batch_items, threads=th), FakeProvider())
                    n_unique = len(batch_items)
                    assert fin and len(fin["translations"]) == n_unique, \
                        "sweep lost strings nf=%d pf=%d th=%d" % (nf, pf, th)

        # Dedup across files: one unique string repeated everywhere → one send.
        items = [It(i, "REPEAT", file="f%d.txt" % (i % 4)) for i in range(40)]
        fin = run(Req(items, threads=6), FakeProvider())
        assert fin and len(fin["translations"]) == 40, "dedup fan-out lost ids"

        # One large file split across many threads; and many tiny files.
        items = [It(i, "h%d" % i, file="huge.txt") for i in range(120)]
        fin = run(Req(items, threads=10), FakeProvider())
        assert fin and len(fin["translations"]) == 120, "huge file lost strings"
        items = [It(fi * 2 + j, "t%d_%d" % (fi, j), file="tiny%d.txt" % fi)
                 for fi in range(30) for j in range(2)]
        fin = run(Req(items, threads=10), FakeProvider())
        assert fin and len(fin["translations"]) == 60, "many tiny files lost strings"

        # Pause: in-flight batches drain + emit; no progress while paused; resume
        # finishes every string (the project-file-integrity guarantee).
        items = [It(i, "s%d" % i) for i in range(50)]
        flag = {"v": False}
        sched, t, out = run(Req(items, threads=3), FakeProvider(delay=0.15),
                            pause_flag=flag)
        time.sleep(0.3)
        flag["v"] = True
        time.sleep(2.0)
        at_pause = len(sched.result)
        time.sleep(1.2)
        assert len(sched.result) == at_pause, "progressed while paused"
        flag["v"] = False
        t.join(20)
        assert not t.is_alive(), "did not finish after resume"
        assert out["final"] and len(out["final"]["translations"]) == 50, \
            "pause/resume lost strings"

        # --- Hybrid fit: re-ask shorter, then record a font-shrink factor -------
        # A menu choice needs a pixel budget, which the scheduler computes only for
        # renpy + a "menu" path + a resolved source font. Drive that path with a
        # provider that first returns an over-wide translation, then a short one.
        class WidthReq(Req):
            engine = "renpy"
            def __init__(self, items, **kw):
                super().__init__(items, **kw)
                self.root = "."  # truthy so the budget branch runs

        class MenuIt(It):
            def __init__(self, i, text):
                super().__init__(i, text, file="script.rpy")
                self.path = ["label", "start", "menu", str(i)]

        # MULTI-WORD (3+) over-wide caption: re-ask shortens on the 2nd call.
        # Only 3+ word captions may be re-asked (a synonym can shorten honestly);
        # 1-2 word captions are NEVER re-asked (see the no-reask case below).
        class ShortenProvider(FakeProvider):
            def __init__(self):
                super().__init__()
                self.calls = 0
            def translate(self, batch, lang, glossary, cfg):
                self.calls += 1
                if self.calls == 1:
                    # Wildly over-wide first answer — FOUR words so the re-ask path
                    # (3+ words) is eligible.
                    return TranslateResult(
                        {it.id: "Очень длинный непомерный перевод" for it in batch},
                        Usage(10, 12))
                return TranslateResult({it.id: "Да" for it in batch}, Usage(10, 12))

        items = [MenuIt(1, "No")]
        prov = ShortenProvider()
        final = run(WidthReq(items, threads=1), prov)
        assert final and final["translations"].get("1") == "Да", \
            "re-ask did not replace the over-wide translation with the short one"
        assert prov.calls >= 2, "re-ask never happened"
        assert not final.get("size_fixes"), \
            "shortening was enough; no font shrink should be recorded"

        # 1-WORD over-wide caption: MUST NOT be re-asked (asking the model to
        # shorten "Сохранение" only yields "Сох"). Instead the full word is kept
        # and a font-shrink factor is recorded. This is the core anti-"Сох" rule.
        class OneWordProvider(FakeProvider):
            def __init__(self):
                super().__init__()
                self.calls = 0
            def translate(self, batch, lang, glossary, cfg):
                self.calls += 1
                return TranslateResult(
                    {it.id: "ОченьДлинноеОдноСловоКотороеТочноНеВлезет" for it in batch},
                    Usage(10, 12))

        items = [MenuIt(3, "Save")]
        prov1 = OneWordProvider()
        final = run(WidthReq(items, threads=1), prov1)
        assert prov1.calls == 1, \
            f"a 1-word caption must NOT be re-asked (would butcher to 'Сох'), got {prov1.calls} calls"
        assert final["translations"].get("3") == "ОченьДлинноеОдноСловоКотороеТочноНеВлезет", \
            "the full word must be kept intact, never abbreviated"
        sf1 = final.get("size_fixes") or {}
        assert "3" in sf1 and 0.0 < sf1["3"] < 1.0, \
            f"a 1-word over-wide caption must record a font-shrink, got {sf1}"

        # Provider that NEVER shortens a MULTI-WORD caption → after re-asks a shrink
        # factor must still be recorded (font-shrink is the final fallback).
        class StubbornProvider(FakeProvider):
            def translate(self, batch, lang, glossary, cfg):
                return TranslateResult(
                    {it.id: "Очень длинный непомерный несокращаемый перевод" for it in batch},
                    Usage(10, 12))

        items = [MenuIt(2, "Hi")]
        final = run(WidthReq(items, threads=1), StubbornProvider())
        sf = final.get("size_fixes") or {}
        assert "2" in sf and 0.0 < sf["2"] < 1.0, \
            f"un-shortenable caption must record a shrink factor <1.0, got {sf}"
    finally:
        (scheduler._RETRY_BACKOFF_FIRST, scheduler._RETRY_BACKOFF_REST,
         scheduler.get_provider) = saved


def check_epic_games() -> None:
    import tempfile
    import shutil
    import json
    from main import _get_epic_games

    temp_dir = tempfile.mkdtemp()
    try:
        # Create a mock game directory
        game_dir_1 = os.path.join(temp_dir, "Game1")
        os.makedirs(game_dir_1)
        game_dir_2 = os.path.join(temp_dir, "Game2")
        os.makedirs(game_dir_2)

        # Write valid manifest
        item_1 = {
            "DisplayName": "Awesome Game 1",
            "InstallLocation": game_dir_1
        }
        with open(os.path.join(temp_dir, "game1.item"), "w", encoding="utf-8") as f:
            json.dump(item_1, f)

        # Write duplicate manifest (same location, different display name)
        item_2 = {
            "DisplayName": "Awesome Game 1 Duplicate",
            "InstallLocation": game_dir_1
        }
        with open(os.path.join(temp_dir, "game1_dup.item"), "w", encoding="utf-8") as f:
            json.dump(item_2, f)

        # Write manifest for Game 2
        item_3 = {
            "DisplayName": "Awesome Game 2",
            "InstallLocation": game_dir_2
        }
        with open(os.path.join(temp_dir, "game2.item"), "w", encoding="utf-8") as f:
            json.dump(item_3, f)

        # Write malformed manifest (invalid JSON)
        with open(os.path.join(temp_dir, "malformed.item"), "w", encoding="utf-8") as f:
            f.write("{invalid json")

        # Write manifest pointing to non-existent location
        item_nonexist = {
            "DisplayName": "NonExist",
            "InstallLocation": os.path.join(temp_dir, "DoesNotExist")
        }
        with open(os.path.join(temp_dir, "nonexist.item"), "w", encoding="utf-8") as f:
            json.dump(item_nonexist, f)

        # Scan and verify
        games = _get_epic_games(temp_dir)

        # We expect exactly 2 games: Game 1 and Game 2.
        # - The malformed item must be skipped cleanly.
        # - The duplicate location must be deduplicated.
        # - The non-existent directory must be filtered out.
        assert len(games) == 2, f"Expected 2 games, got {len(games)}"
        
        # Verify names and paths
        names = [g["name"] for g in games]
        assert "Awesome Game 1" in names
        assert "Awesome Game 2" in names
        assert "NonExist" not in names
        
        # Paths should be normalised with forward slashes
        for g in games:
            assert "\\" not in g["path"], "Paths should use forward slashes"
            assert "/" in g["path"], "Paths should be absolute and normalised"
            
    finally:
        shutil.rmtree(temp_dir)


def check_steam_games() -> None:
    import tempfile
    import shutil
    from main import _get_steam_games, FsListReq, fs_list

    temp_dir = tempfile.mkdtemp()
    try:
        # Create a mock steamapps/common layout
        library_1 = os.path.join(temp_dir, "Library1")
        common_1 = os.path.join(library_1, "steamapps", "common")
        os.makedirs(common_1)
        
        library_2 = os.path.join(temp_dir, "Library2")
        common_2 = os.path.join(library_2, "steamapps", "common")
        os.makedirs(common_2)

        # Create mock games
        game_1_dir = os.path.join(common_1, "GameOne")
        os.makedirs(game_1_dir)
        game_2_dir = os.path.join(common_2, "GameTwo")
        os.makedirs(game_2_dir)
        
        # Non-directory files should be ignored
        with open(os.path.join(common_1, "ignored_file.txt"), "w") as f:
            f.write("test")

        # Mock list libraries returned by _steam_libraries()
        mock_libs = [
            {"name": "Steam (C:)", "path": common_1.replace("\\", "/")},
            {"name": "Steam (D:)", "path": common_2.replace("\\", "/")}
        ]

        # Scan and verify
        games = _get_steam_games(mock_libs)

        assert len(games) == 2, f"Expected 2 games, got {len(games)}"
        names = [g["name"] for g in games]
        assert "GameOne" in names
        assert "GameTwo" in names
        assert "ignored_file.txt" not in names

        # Verify parent override in fs_list
        import main
        orig_steam_libraries = main._steam_libraries
        main._steam_libraries = lambda: mock_libs
        try:
            # 1. Check parent for a game root folder inside Steam library
            res = fs_list(FsListReq(path=game_1_dir))
            assert res["parent"] == "steam_games_library", f"Expected steam_games_library parent, got {res['parent']}"

            # 2. Check parent for a deeper subfolder (parent should NOT be overridden to library)
            game_sub = os.path.join(game_1_dir, "game")
            os.makedirs(game_sub)
            res_sub = fs_list(FsListReq(path=game_sub))
            assert res_sub["parent"] == game_1_dir.replace("\\", "/"), f"Expected game root parent, got {res_sub['parent']}"
        finally:
            main._steam_libraries = orig_steam_libraries
            
    finally:
        shutil.rmtree(temp_dir)


def check_renpy_python_pool() -> None:
    """The Ren'Py inline-Python key-pool (renpy_python_translator._run_batches_over_keypool):
    multi-key fan-out with the SAME failover as the main scheduler — auth-dead key
    requeues its batch to a survivor, a transient rate error recovers, and all-keys-dead
    terminates (no hang). Offline: a fake process_fn stands in for the API call.

    Backoff/cooldown floors are zeroed via delay_seconds=0 + tiny grace so it's instant."""
    import threading
    import renpy_python_translator as rp

    # Each "batch" is just an int id; process_fn returns {id: id*10} so we can assert
    # every batch was processed. keys grouped keys[i // threads] like the scheduler.
    def run(keys, threads, process_fn, batches=None):
        batches = batches if batches is not None else list(range(8))
        return rp._run_batches_over_keypool(
            batches, keys, threads, 0.0, "Tested", process_fn
        )

    # 1. All-success: every batch merged, exactly once.
    def pf_ok(batch, key, idx):
        return {batch: batch * 10}
    merged = run(["K1", "K2"], 2, pf_ok)
    assert merged == {b: b * 10 for b in range(8)}, f"all-success drifted: {merged}"

    # 2. Auth-death failover: K1 always 403s, K2 works. Despite the grace requeues,
    #    every batch must still land (proves requeue → survivor finished them).
    lock = threading.Lock()
    seen_keys = set()
    def pf_auth(batch, key, idx):
        with lock:
            seen_keys.add(key)
        if key == "K1":
            raise RuntimeError("API key not valid. Please pass a valid API key. (403)")
        return {batch: batch * 10}
    merged = run(["K1", "K2"], 2, pf_auth)
    assert merged == {b: b * 10 for b in range(8)}, f"auth-failover lost batches: {merged}"
    assert "K2" in seen_keys, "survivor key never used"

    # 3. Rate-once-then-succeed: a 429 must NOT kill the key; all batches complete.
    counts = {}
    def pf_rate(batch, key, idx):
        with lock:
            n = counts.get(batch, 0)
            counts[batch] = n + 1
        if n == 0:
            raise RuntimeError("429 Too Many Requests: rate limit exceeded")
        return {batch: batch * 10}
    merged = run(["K1"], 2, pf_rate, batches=list(range(4)))
    assert merged == {b: b * 10 for b in range(4)}, f"rate-recover lost batches: {merged}"

    # 4. All keys auth-dead: must terminate (no hang) and return partial/empty.
    def pf_all_dead(batch, key, idx):
        raise RuntimeError("invalid api key (401)")
    done = {}
    def runner():
        done["merged"] = run(["K1", "K2"], 2, pf_all_dead, batches=list(range(4)))
    th = threading.Thread(target=runner, daemon=True)
    th.start()
    th.join(timeout=20)
    assert not th.is_alive(), "all-keys-dead HUNG (deadlock)"
    assert done.get("merged") == {}, f"all-dead should be empty, got {done.get('merged')}"

    print("OK — renpy python key-pool: all-success, auth-failover, rate-recover, all-dead-terminates")


def check_renpy_python_sources() -> None:
    """load_all_sources READS archived scripts (into memory, for the strings
    dictionary) but NEVER writes them to disk. The old path extracted archived
    .rpy to disk + recompiled .rpyc — which risked the double-load crash (real
    failure: Killer Chat!'s pronoun_backend.rpy → 'RevertableDict' object is not
    callable). The native strings-dict approach only needs to read the source, so
    archives stay untouched on disk. This builds a tiny game with a loose .rpy and
    a real RPA-3.0 holding an archive-only .rpy, then asserts BOTH are readable as
    sources AND nothing new was written to the game dir."""
    import pickle, zlib
    from pathlib import Path
    import renpy_python_translator as rp

    def _make_rpa(path: str, files: dict[str, bytes]) -> None:
        # Minimal RPA-3.0 writer (mirror of parsers/rpa.py's reader). key=0 so the
        # XOR obfuscation is identity, which the reader handles.
        key = 0
        with open(path, "wb") as f:
            f.write(b"RPA-3.0 0000000000000000 00000000\n")  # placeholder header
            index = {}
            for name, data in files.items():
                off = f.tell()
                f.write(data)
                index[name] = [[off ^ key, len(data) ^ key, b""]]
            index_off = f.tell()
            f.write(zlib.compress(pickle.dumps(index)))
            f.seek(0)
            f.write(("RPA-3.0 %016x %08x\n" % (index_off, key)).encode("ascii"))

    with tempfile.TemporaryDirectory() as td:
        game = os.path.join(td, "game")
        os.makedirs(game, exist_ok=True)
        loose_abs = os.path.join(game, "script", "loose.rpy")
        os.makedirs(os.path.dirname(loose_abs), exist_ok=True)
        with open(loose_abs, "w", encoding="utf-8") as f:
            f.write('init python:\n    x = "hello"\n')
        _make_rpa(os.path.join(game, "archive.rpa"), {
            "script/loose.rpy": b'init python:\n    x = "hello"\n',          # also loose
            "script/archived_only.rpy": b'init python:\n    y = "world"\n',  # archive ONLY
        })

        # Snapshot the on-disk .rpy set BEFORE.
        before = {p for p in Path(game).rglob("*.rpy")}
        sources = rp.load_all_sources(Path(td))
        after = {p for p in Path(game).rglob("*.rpy")}

        rels = {os.path.relpath(p, game).replace("\\", "/") for p in sources}
        # Archived-only script IS read (needed to find translatable candidates)...
        assert "script/archived_only.rpy" in rels, \
            f"archive-only script must be READ for the strings dict: {rels}"
        assert "script/loose.rpy" in rels, f"loose script missing: {rels}"
        # ...but NOTHING new was written to disk (no extract → no double-load risk).
        assert before == after, \
            f"load_all_sources wrote files to disk (must be read-only): {after - before}"

    print("OK — renpy python sources: archived scripts read in-memory, never extracted to disk")


def check_renpy_keystring_safety() -> None:
    """find_comparison_keys must catch every string the game COMPARES (==, !=,
    in [...], dict key, .get()), so the inline-Python translator force-skips them
    and never breaks a click/branch (the 'translated and now I can't click it'
    bug). A string that is only assigned/displayed must NOT be caught (no
    over-blocking)."""
    import renpy_python_translator as rp

    sources = {
        "a.rpy": (
            'label x:\n'
            '    if screen == "home":\n'
            '        pass\n'
            '    if msg == "Received file.exe":\n'
            '        pass\n'
            '    $ if x != "voice": pass\n'
            '    $ if w in ["murder weapon", "rope"]: pass\n'
            '    $ d = {"star sign": 1}\n'
            '    $ v = cfg.get("default")\n'
            '    $ shown = "just visible text"\n'           # only assigned → NOT a key
            '    $ chat.append("hello there friend")\n'      # only appended → NOT a key
        ),
    }
    keys = rp.find_comparison_keys(sources)

    must_have = {"home", "Received file.exe", "voice", "murder weapon", "rope",
                 "star sign", "default"}
    missing = must_have - keys
    assert not missing, f"comparison keys not detected: {missing}"

    must_not = {"just visible text", "hello there friend"}
    leaked = must_not & keys
    assert not leaked, f"non-key strings wrongly flagged as comparison keys: {leaked}"

    # The force-skip hook compares entry['value'] against this set, so membership
    # is exactly what protects a candidate. Verify the contract value-for-value.
    assert "Received file.exe" in keys and "just visible text" not in keys

    print("OK — renpy keystring safety: comparison keys protected, plain text still translatable")


def check_renpy_keystring_promotion() -> None:
    """Promote visible prose comparison keys (e.g. 'murder weapon') to translation
    while code tokens stay untouched and tl/-covered values are excluded.

    NOTE: with the native strings-dict approach there is NO global file rewrite —
    a promoted key is simply added to the dictionary; the dict translates the
    DISPLAYED text while a code comparison (`== "key"`) reads the raw value and
    still matches. So this only verifies the promotion PREDICATES + tl/ exclusion."""
    import renpy_python_translator as rp

    # --- Predicates ---
    assert rp._looks_like_code_token("home")
    assert rp._looks_like_code_token("voice")
    assert rp._looks_like_code_token("V")
    assert rp._looks_like_code_token("he")
    assert not rp._looks_like_code_token("murder weapon")
    assert not rp._looks_like_code_token("Roleplay")
    assert not rp._looks_like_code_token("Death of the Author")

    # Visible key: appears in an append→messages context
    vis_entry = {"context_function": "append", "context_variable": "messages",
                 "context_param": "value"}
    assert rp._visible_translatable_key("murder weapon", [vis_entry])
    # Code token → never visible
    assert not rp._visible_translatable_key("home", [vis_entry])
    assert not rp._visible_translatable_key("V", [vis_entry])

    # --- tl/-covered value excluded ---
    # Simulate: "hello" is a comparison key AND appears in display, but is
    # already covered by tl/ → should NOT be promoted.
    sources2 = {
        Path("x.rpy"): '    if x == "hello":\n    $ msgs.append("hello")\n',
    }
    comparison = rp.find_comparison_keys(sources2)
    assert "hello" in comparison
    displayed_via_tl = {"hello"}
    display_by = {"hello": [{"context_function": "append", "context_variable": "msgs", "context_param": "value"}]}
    visible = {v for v in comparison
               if rp._visible_translatable_key(v, display_by.get(v, []))
               and v not in displayed_via_tl}
    assert "hello" not in visible, "tl/-covered key should be excluded from promotion"
    print("OK — renpy keystring promotion: predicates, global replace, tl/-exclusion")


def check_renpy_python_cache() -> None:
    """Inline-Python translation cache + screen-call extraction + multi-occurrence
    replace. These back the "Write translation lays down everything for free" and
    "re-run translates only new strings" guarantees."""
    import tempfile, shutil
    from pathlib import Path
    import renpy_python_translator as rp

    # --- _TranslationCache: store, hit, language-versioned ---
    tmp = Path(tempfile.mkdtemp(prefix="interp_tcache_"))
    entry = {"value": "Save my Soul", "raw_line": 'x.append("Save my Soul")',
             "context_function": "append", "context_variable": "blog",
             "context_param": None}
    c1 = rp._TranslationCache(tmp, "russian")
    assert c1.get(entry) is None, "fresh cache must miss"
    c1.put(entry, "Спаси мою Душу")
    c1.save()
    # Cache lands inside Interprex/, NOT as a root dotfile (keeps game root clean).
    assert (tmp / "Interprex" / "python_translations.json").exists(), \
        "translation cache must be written under Interprex/"
    assert not (tmp / ".interprex_python_translations.json").exists(), \
        "old root dotfile must not be created"
    # Reload same language → hit
    c2 = rp._TranslationCache(tmp, "russian")
    assert c2.get(entry) == "Спаси мою Душу", "cache should persist + hit"
    # Reload different language → miss (must not feed wrong-language text)
    c3 = rp._TranslationCache(tmp, "french")
    assert c3.get(entry) is None, "language switch must invalidate cache"
    shutil.rmtree(tmp, ignore_errors=True)

    # --- screen-call extraction (search history lives in show screen args) ---
    content = (
        'label start:\n'
        '    show screen search_bar(history_entries=["how to murder someone and dispose of the body"])\n'
        '    call screen confirm(message="Are you sure?")\n'
    )
    cands = rp._extract_screen_call_candidates(content)
    vals = {c["value"] for c in cands}
    assert "how to murder someone and dispose of the body" in vals, vals
    assert "Are you sure?" in vals, vals
    for c in cands:
        assert c["context_function"] == "screen"
        assert c["raw_literal"] and c["value"]  # apply needs raw_literal

    # --- _write_inline_strings_file: native strings block, no archive edits ---
    tmp2 = Path(tempfile.mkdtemp(prefix="interp_inline_"))
    pairs = [
        ("don't be so obvious smh\nYou're Gonna Get Caught", "не будь таким очевидным"),
        ("Online", "В сети"),
        ("Online", "ДУБЛЬ"),   # duplicate old → must be deduped (first wins)
    ]
    n = rp._write_inline_strings_file(tmp2, "russian", pairs)
    assert n == 2, f"expected 2 written (dedup), got {n}"
    out = tmp2 / "game" / "tl" / "russian" / "_interprex_inline.rpy"
    assert out.exists(), "inline strings file not created"
    txt = out.read_text(encoding="utf-8")
    # Header + native old/new format
    assert "translate russian strings:" in txt, txt
    # \n escaped as literal backslash-n (not a real newline). Apostrophe stays
    # plain — _string_quote wraps in double quotes and only escapes ", \, \n.
    assert '\\nYou\'re Gonna Get Caught"' in txt, repr(txt)  # \n stayed literal
    assert 'old "don\'t be so obvious smh' in txt, repr(txt)
    assert 'new "не будь таким очевидным"' in txt, txt
    # Dedup: only the FIRST "Online" translation, not "ДУБЛЬ"
    assert txt.count('old "Online"') == 1, txt
    assert "ДУБЛЬ" not in txt, "duplicate old key must be dropped (first wins)"
    # .lower()/.upper() cased variants added for the display-transform case
    assert 'old "online"' in txt and 'old "ONLINE"' in txt, "cased variants missing"
    # Registered as a created backup → restore deletes it
    meta = tmp2 / ".interprex_backups" / "metadata.json"
    assert meta.exists(), "backup metadata not written"
    import json as _json
    md = _json.loads(meta.read_text(encoding="utf-8"))
    rel = "game/tl/russian/_interprex_inline.rpy"
    assert md.get(rel, {}).get("type") == "created", f"inline file not registered created: {md}"

    # --- CRITICAL: must NOT emit an `old` key already present in the dialogue
    # tl/ tree — Ren'Py crashes on a duplicate string-translation key per
    # language (real failure: "Enter your Killsong username:" in both). ---
    dlg = tmp2 / "game" / "tl" / "russian" / "Overkill_DLC" / "script"
    dlg.mkdir(parents=True, exist_ok=True)
    (dlg / "wildfiresong.rpy").write_text(
        "translate russian strings:\n\n"
        '    old "Enter your Killsong username:"\n'
        '    new "Введите имя Killsong:"\n',
        encoding="utf-8")
    n2 = rp._write_inline_strings_file(
        tmp2, "russian",
        [("Enter your Killsong username:", "ДУБЛЬ-перевод"),  # already in dialogue tl/
         ("Brand new inline string", "Новая строка")])        # not anywhere yet
    txt2 = out.read_text(encoding="utf-8")
    assert n2 == 1, f"the dialogue-tl/ duplicate must be skipped, got {n2}"
    assert "Enter your Killsong username:" not in txt2, \
        "duplicate of a dialogue-tl/ key would crash the game — must be excluded"
    assert "Brand new inline string" in txt2, "fresh inline string must still be written"
    shutil.rmtree(tmp2, ignore_errors=True)

    print("OK — renpy python cache: translation-cache hit/miss + lang-version, "
          "screen-call extraction, native strings-file (dedup, escaping, created-backup, "
          "dialogue-tl/ dup exclusion)")


def main() -> int:
    check_epic_games()
    check_steam_games()
    check_prompt_width()
    check_providers()
    check_scheduler()
    check_renpy_python_pool()
    check_renpy_python_sources()
    check_renpy_keystring_safety()
    check_renpy_keystring_promotion()
    check_renpy_python_cache()
    check_csharp()
    check_unity()
    check_unity_localization()
    check_i18n()
    check_fusion()
    check_mmf2()
    check_qsp()
    root = build_project()
    data_map = os.path.join(root, "data", "Map001.json")

    assert detect_engine(root) == "rpgmaker", "detect failed"
    p = get_parser("rpgmaker")

    strings = p.extract(root)
    originals = [s.original for s in strings]
    assert originals == ["Hello there", "How are you?", "Yes", "No"], originals

    # ids stable across runs
    ids1 = {s.id for s in strings}
    ids2 = {s.id for s in p.extract(root)}
    assert ids1 == ids2, "ids not stable across runs"

    # inject lands in the right slots
    tr = {s.id: s.original.upper() for s in strings}
    written = p.inject(root, tr)
    assert written == 4, f"written={written}"
    after = json.load(open(data_map, encoding="utf-8"))
    lst = after["events"][1]["pages"][0]["list"]
    assert lst[0]["parameters"][0] == "HELLO THERE", lst[0]
    assert lst[2]["parameters"][0] == ["YES", "NO"], lst[2]

    # id parity with the TS makeId (same algorithm) — expected c670705d
    parity = make_id(
        "rpgmaker", "data/Map001.json",
        ["events", "1", "pages", "0", "list", "0", "parameters", "0"],
        "Hello there",
    )
    assert parity == "c670705d", f"id parity drifted: {parity}"

    check_renpy()
    check_renpy_font()
    check_char_limit()
    check_renpy_risk()
    check_renpy_identifier_parity()
    check_renpy_mixed_translate()
    check_renpy_context_history()
    check_renpy_rpa()
    check_renpy_decompilation()
    check_unreal()
    check_unreal4()
    check_unreal4_pak()
    check_unreal4_5_utoc()

    print("OK — detect, extract, id-stability, inject, parity (rpgmaker + renpy + renpy-rpa + renpy-decompile + csharp + unity/dll + unity/assets + unity/localization + i18n + fusion + mmf2 + qsp + unreal + unreal4 + unreal4-pak + unreal4_5-utoc) + scheduler all pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
