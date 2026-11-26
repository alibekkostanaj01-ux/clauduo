"""Окно настроек: ключи в .env и модели ролей в config.yaml — на временных копиях, реальные файлы не трогаем."""
import shutil

import yaml

from g2c import settings as S


def test_set_role_models_keeps_comments(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    shutil.copy(S.CONFIG_FILE, cfg)
    monkeypatch.setattr(S, "CONFIG_FILE", cfg)
    S.config.cache_clear()
    S.set_role_models({"structurer": "gemini-3.8-flash", "fast": "nvidia/x:free"})
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["llm"]["roles"]["structurer"] == "gemini-3.8-flash" and data["llm"]["roles"]["fast"] == "nvidia/x:free"
    assert "# учебный слой" in cfg.read_text(encoding="utf-8")   # комментарии на месте
    S.config.cache_clear()


def test_save_env_replaces_only_its_line(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=old\nOTHER=1\n", encoding="utf-8")
    monkeypatch.setattr(S, "ENV_FILE", env)
    S.save_env("GEMINI_API_KEY", "new-key-123")
    text = env.read_text(encoding="utf-8")
    assert "GEMINI_API_KEY=new-key-123" in text and "OTHER=1" in text and "old" not in text
    assert S.mask("fake-test-key-123456") == "fake…3456"
