"""Test adapter interface compliance for reference implementations."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from adapters import web_app, teaching, chat_agent


REQUIRED_FUNCTIONS = ["setup", "run_checks", "teardown"]


class TestWebAppAdapter:
    def test_has_required_functions(self):
        for fn in REQUIRED_FUNCTIONS:
            assert hasattr(web_app, fn), f"web_app missing {fn}"
            assert callable(getattr(web_app, fn))

    def test_has_prerequisites(self):
        assert hasattr(web_app, 'prerequisites')
        assert isinstance(web_app.prerequisites, list)

    def test_setup_returns_dict(self, tmp_path):
        result = web_app.setup(str(tmp_path))
        assert isinstance(result, dict)
        assert "status" in result
        assert result["status"] in ("ready", "crash")

    def test_run_checks_returns_dict(self, tmp_path):
        result = web_app.run_checks(str(tmp_path), "test_feature")
        assert isinstance(result, dict)
        assert "scores" in result
        assert "details" in result
        assert isinstance(result["scores"], dict)

    def test_teardown_accepts_dict(self):
        web_app.teardown({})
        web_app.teardown({"pid": None})

    def test_no_removed_functions(self):
        """Old interface functions should be removed."""
        removed = ["get_dimensions", "get_eval_prompt", "get_eval_output_format",
                    "get_eval_summary_format", "get_builder_instructions",
                    "get_planner_instructions", "get_prerequisite_checks"]
        for fn in removed:
            assert not hasattr(web_app, fn), f"web_app still has removed function {fn}"


class TestTeachingAdapter:
    def test_has_required_functions(self):
        for fn in REQUIRED_FUNCTIONS:
            assert hasattr(teaching, fn), f"teaching missing {fn}"
            assert callable(getattr(teaching, fn))

    def test_has_prerequisites(self):
        assert hasattr(teaching, 'prerequisites')
        assert isinstance(teaching.prerequisites, list)

    def test_setup_creates_workspace(self, tmp_path):
        result = teaching.setup(str(tmp_path))
        assert result["status"] == "ready"
        workspace = Path(result["info"]["workspace"])
        assert workspace.exists()
        assert ".evolve" in str(workspace)

    def test_run_checks_empty_scores(self, tmp_path):
        """Teaching has no deterministic scoring."""
        result = teaching.run_checks(str(tmp_path), "test_feature")
        assert result["scores"] == {}

    def test_teardown_noop(self):
        teaching.teardown({})

    def test_no_removed_functions(self):
        removed = ["get_dimensions", "get_eval_prompt", "get_eval_output_format",
                    "get_eval_summary_format", "get_builder_instructions",
                    "get_planner_instructions", "get_prerequisite_checks",
                    "get_features_from_program"]
        for fn in removed:
            assert not hasattr(teaching, fn), f"teaching still has removed function {fn}"


class TestChatAgentAdapter:
    def test_has_required_functions(self):
        for fn in REQUIRED_FUNCTIONS:
            assert hasattr(chat_agent, fn), f"chat_agent missing {fn}"
            assert callable(getattr(chat_agent, fn))

    def test_has_prerequisites(self):
        assert hasattr(chat_agent, 'prerequisites')
        assert isinstance(chat_agent.prerequisites, list)

    def test_run_checks_missing_scenario(self, tmp_path):
        """run_checks returns error when scenario config is missing."""
        result = chat_agent.run_checks(str(tmp_path), "nonexistent")
        assert result["scores"] == {}
        assert "ERROR" in result["details"]

    def test_teardown_noop(self):
        chat_agent.teardown({})

    def test_load_config_defaults(self, tmp_path):
        """Config defaults are used when adapter_config.json is missing."""
        config = chat_agent._load_config(str(tmp_path))
        assert config["agent_name"] == "my-agent"
        assert config["default_rounds"] == 8
        assert "api_url" in config.get("simulator_api_url", "")  or "openai" in config.get("simulator_api_url", "")

    def test_load_config_from_file(self, tmp_path):
        """Config is loaded from adapter_config.json when present."""
        evolve_dir = tmp_path / ".evolve"
        evolve_dir.mkdir()
        config_file = evolve_dir / "adapter_config.json"
        config_file.write_text(json.dumps({
            "agent_name": "test-bot",
            "agent_cmd": "my-cli",
            "simulator_model": "gpt-4o-mini",
            "default_rounds": 5,
        }))
        config = chat_agent._load_config(str(tmp_path))
        assert config["agent_name"] == "test-bot"
        assert config["default_rounds"] == 5

    def test_build_simulator_prompt(self):
        """Simulator prompt includes persona and theme."""
        prompt = chat_agent._build_simulator_prompt(
            "Alice, 22, college student", "exam stress", "anxious", "Buddy"
        )
        assert "Alice" in prompt
        assert "exam stress" in prompt
        assert "anxious" in prompt
        assert "Buddy" in prompt
