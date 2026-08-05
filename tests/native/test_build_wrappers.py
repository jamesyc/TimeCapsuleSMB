import tempfile
from pathlib import Path
import pytest
from tests.test_mdns_build import MdnsBuildWrapperTests
from tests.native.build import binary_name

@pytest.mark.parametrize('name', ['mdns', 'nbns', 'service', 'telemetry'])
@pytest.mark.parametrize('suffix,triple', [('', 'arm--netbsdelf'), ('oldle', 'arm--netbsdelf'), ('oldbe', 'armeb--netbsdelf')])
def test_compiler_failure_does_not_repackage_stale_output(name, suffix, triple):
    helper = MdnsBuildWrapperTests()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        env, _, _, _ = helper.env_for(root, triple=triple)
        env[name.upper() + '_STAGE'] = str(root / 'stage')
        env[name.upper() + '_LOG'] = str(root / 'failure.log')
        stage = root / 'stage'; stage.mkdir()
        (stage / binary_name(name)).write_bytes(b'old executable')
        helper.make_executable(root / 'out/tools/bin' / (triple + '-gcc'), '#!/bin/sh\nexit 19\n')
        result = helper.run_wrapper(name + suffix + '.sh', env)
        assert result.returncode != 0
        assert not (stage / (binary_name(name) + '.stripped')).exists()
