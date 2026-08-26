"""launchd 서비스 구성의 핵심 안전 경계를 정적으로 검증한다."""
from pathlib import Path


SCRIPT = (Path(__file__).resolve().parents[1] / "scripts" / "agent_services.sh").read_text(
encoding="utf-8"
)


def test_data_api_is_loopback_only_launchd_service():
    assert 'LABEL_DATA_API="com.hyunjun.ai-agent.data-api"' in SCRIPT
    assert "<string>${SCRIPTS}/data_api.py</string>" in SCRIPT
    assert "<string>127.0.0.1</string>" in SCRIPT
    assert "<string>8090</string>" in SCRIPT


def test_private_data_api_is_distinct_authenticated_launchd_service():
    assert 'LABEL_PRIVATE_DATA_API="com.hyunjun.ai-agent.private-data-api"' in SCRIPT
    begin = SCRIPT.index("write_plist_private_data_api()")
    end = SCRIPT.index("write_plist_watch()", begin)
    plist = SCRIPT[begin:end]
    assert "<string>${SCRIPTS}/private_data_api.py</string>" in plist
    assert "<string>127.0.0.1</string>" in plist
    assert "<string>8091</string>" in plist
    assert "AI_AGENT_PRIVATE_API_TOKEN" not in plist
    assert "TELEGRAM_BOT_TOKEN" not in plist
    assert "AI_AGENT_PRIVATE_WRITE_BUNDLE_PATH" not in plist


def test_consumers_enable_api_first_with_local_fallback():
    telegram = SCRIPT[SCRIPT.index("write_plist_telegram()") : SCRIPT.index("write_plist_paper()")]
    paper = SCRIPT[SCRIPT.index("write_plist_paper()") : SCRIPT.index("write_plist_weekly()")]
    watch = SCRIPT[SCRIPT.index("write_plist_watch()") : SCRIPT.index("write_plist_telegram()")]
    assert "<key>AI_AGENT_DATA_API_ENABLED</key>" in telegram
    assert "<key>AI_AGENT_PRIVATE_API_ENABLED</key>" in telegram
    assert "<key>AI_AGENT_DATA_API_ENABLED</key>" in paper
    assert "<key>AI_AGENT_PRIVATE_API_ENABLED</key>" in paper
    assert "AI_AGENT_PRIVATE_API_TOKEN" not in paper
    assert "AI_AGENT_PRIVATE_WRITE_BUNDLE_PATH" not in telegram
    assert "<key>AI_AGENT_DATA_API_ENABLED</key>" not in watch
    assert "소비자는 기존 로컬 DB로 fallback" in SCRIPT


def test_data_api_is_loaded_before_consumers():
    start = SCRIPT.index("cmd_start()")
    body = SCRIPT[start:SCRIPT.index("cmd_stop()", start)]
    api_load = body.index('launchctl load "$PLIST_DATA_API"')
    private_api_load = body.index('launchctl load "$PLIST_PRIVATE_DATA_API"')
    watch_load = body.index('launchctl load "$PLIST_WATCH"')
    telegram_load = body.index('launchctl load "$PLIST_TG"')
    paper_load = body.index('launchctl load "$PLIST_PAPER"')
    assert api_load < private_api_load < watch_load < telegram_load < paper_load


def test_private_api_has_dedicated_install_and_readiness():
    assert "cmd_install_private_data_api()" in SCRIPT
    assert '"http://127.0.0.1:8091/health"' in SCRIPT
    assert "install-private-data-api) cmd_install_private_data_api" in SCRIPT


def test_telegram_has_dedicated_private_api_restart():
    assert "cmd_restart_telegram()" in SCRIPT
    assert "restart-telegram) cmd_restart_telegram" in SCRIPT


def test_market_data_collector_is_scheduled_and_managed():
    assert 'LABEL_MARKET_COLLECTOR="com.hyunjun.ai-agent.market-data-collector"' in SCRIPT
    assert "<string>${SCRIPTS}/market_data_collector.py</string>" in SCRIPT
    assert "<string>--dataset</string>" in SCRIPT
    assert "<integer>16</integer>" in SCRIPT
    assert "<integer>20</integer>" in SCRIPT
    assert 'launchctl load "$PLIST_MARKET_COLLECTOR"' in SCRIPT
