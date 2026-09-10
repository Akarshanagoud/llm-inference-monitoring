import pytest

from llmobs import GatewayConfig, InferenceGateway, LocalSimBackend


@pytest.fixture
def backend():
    # speed=500 compresses the simulated generation so tests stay fast while
    # keeping the relative shape of prefill vs decode intact.
    return LocalSimBackend(speed=500, seed=42)


@pytest.fixture
def gateway(backend, tmp_path):
    gw = InferenceGateway(
        backend, GatewayConfig(trace_store_path=str(tmp_path / "traces.db"))
    )
    yield gw
    gw.close()
