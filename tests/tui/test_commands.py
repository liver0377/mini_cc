from __future__ import annotations

from mini_cc.tui.commands import BUILTIN_COMMANDS, match_commands


class TestMatchCommands:
    def test_no_match_without_slash(self):
        assert match_commands("help") == []

    def test_exact_prefix_match(self):
        results = match_commands("/he")
        assert len(results) == 1
        assert results[0].name == "/help"

    def test_full_command_match(self):
        results = match_commands("/exit")
        assert len(results) == 1
        assert results[0].name == "/exit"

    def test_slash_only_matches_all(self):
        results = match_commands("/")
        assert len(results) == len(BUILTIN_COMMANDS)

    def test_no_match_for_gibberish(self):
        results = match_commands("/xyzabc")
        assert len(results) == 0

    def test_description_keyword_match(self):
        results = match_commands("/压缩")
        assert any(cmd.name == "/compact" for cmd in results)

    def test_description_keyword_mode(self):
        results = match_commands("/模式")
        assert any(cmd.name == "/mode" for cmd in results)

    def test_description_keyword_exit(self):
        results = match_commands("/退出")
        assert any(cmd.name == "/exit" for cmd in results)

    def test_prefix_takes_priority_over_description(self):
        results = match_commands("/c")
        names = [cmd.name for cmd in results]
        assert names[0] == "/compact" or names[0] == "/clear"
