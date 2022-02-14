import pytest


@pytest.mark.asyncio
async def test_setup(setup_info):  # noqa
    try:
        pass
    finally:
        await setup_info.sim.close()
