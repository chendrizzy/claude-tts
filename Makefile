# TTS Pipeline — make targets.
#
# `make verify` is the BINDING QUALITY GATE (DIAGNOSIS H0): the machine-checkable
# definition of "done" that the project previously lacked (the listen-test gate
# kept getting skipped, so regressions shipped). It is deterministic, needs no
# live daemon / Ollama / pytest-asyncio, and FAILS on:
#   - markdown leaking to the voice (R1)            tests/test_spoken_render.py
#   - the inline-code deletion bug / non-idempotence (R1)
#   - markup in the real shadow.log corpus (R1)     tests/test_shadow_replay.py
#   - path-humanization regressions                 tests/test_text_utils.py
#   - SPEAK/SKIP classification regressions         tests/test_router_corpus.py
#
# Wire this into any "is it done?" check instead of relying on human memory.

PYTHON ?= python3

.PHONY: verify verify-all verify-install sweep-logs restart manifests help

help:
	@echo "make verify      - binding quality gate (deterministic, no live deps)"
	@echo "make verify-all  - full test suite (async tests need: pip install pytest-asyncio)"
	@echo "make verify-install - install-readiness dry-run (layout, perms, setup modules)"
	@echo "make sweep-logs  - idempotent TTS log retention sweep (R3 hygiene)"
	@echo "make restart     - safe daemon restart (picks up on-disk code; resets health uptime)"
	@echo "make manifests   - sync plugin manifest versions from pyproject.toml"

verify:
	$(PYTHON) tests/fixtures/spoken_corpus/_generate.py
	$(PYTHON) -m pytest -q \
		tests/test_spoken_render.py \
		tests/test_shadow_replay.py \
		tests/test_text_utils.py \
		tests/test_router_corpus.py \
		tests/test_queue_manager.py \
		tests/test_content_router.py \
		tests/test_voicebox_client.py \
		tests/test_cache_cleanup.py \
		tests/test_llm_provider.py \
		tests/test_tts_engines.py \
		tests/test_platform.py \
		tests/test_paths.py \
		tests/test_plugin_manifest.py \
		tests/test_calibration.py \
		tests/test_service_render.py \
		tests/test_systemd_render.py \
		tests/test_config_render.py

verify-all:
	$(PYTHON) -m pytest -q tests

verify-install:
	bash tests/test-install-dry-run.sh

sweep-logs:
	sh scripts/sweep_tts_logs.sh

manifests:
	$(PYTHON) scripts/build_manifests.py

restart:
	sh restart_daemon.sh
