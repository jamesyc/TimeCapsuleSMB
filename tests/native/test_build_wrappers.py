import tempfile
from pathlib import Path
import pytest
from tests.build_wrapper_harness import BuildWrapperHarness

@pytest.mark.parametrize('suffix,triple', [('', 'arm--netbsdelf'), ('oldle', 'arm--netbsdelf'), ('oldbe', 'armeb--netbsdelf')])
def test_compiler_failure_does_not_repackage_stale_service(suffix, triple):
    helper = BuildWrapperHarness()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        env, _, _, _ = helper.env_for(root, triple=triple)
        env['SERVICE_STAGE'] = str(root / 'stage')
        env['SERVICE_LOG'] = str(root / 'failure.log')
        stage = root / 'stage'; stage.mkdir()
        (stage / 'service').write_bytes(b'old executable')
        helper.make_executable(root / 'out/tools/bin' / (triple + '-gcc'), '#!/bin/sh\nexit 19\n')
        result = helper.run_wrapper('service' + suffix + '.sh', env)
        assert result.returncode != 0
        assert not (stage / 'service.stripped').exists()
