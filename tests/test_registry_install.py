"""Only a missing new-version index entry can be retried, within a fixed bound."""
import subprocess
import pytest
from scripts.install_registry_sdk import install


@pytest.mark.parametrize('registry,missing',[('pypi','No matching distribution found for pliqra-client==0.1.1'),('npm','npm error code ETARGET')])
def test_install_waits_for_version_visibility_then_requires_real_success(registry,missing):
    calls=[];clock=[0]
    def runner(command,**kwargs):
        calls.append(command)
        assert kwargs['timeout']==60 and kwargs['capture_output']
        return subprocess.CompletedProcess(command,1 if len(calls)==1 else 0,'',missing if len(calls)==1 else '')
    install(['canonical-installer'],registry,runner=runner,clock=lambda:clock[0],sleep=lambda n:clock.__setitem__(0,clock[0]+n))
    assert len(calls)==2 and clock[0]==10


@pytest.mark.parametrize('registry',['pypi','npm'])
def test_install_does_not_retry_auth_tls_or_integrity_failure(registry):
    def runner(command,**kwargs):return subprocess.CompletedProcess(command,1,'','TLS verification or package integrity failed')
    def no_sleep(n):raise AssertionError('Non-visibility errors must fail immediately')
    with pytest.raises(subprocess.CalledProcessError):
        install(['canonical-installer'],registry,runner=runner,sleep=no_sleep)


@pytest.mark.parametrize('registry,missing',[('pypi','No matching distribution found for pliqra-client==0.1.1'),('npm','npm error code ETARGET')])
def test_install_visibility_wait_is_bounded_and_still_fails(registry,missing):
    clock=[0];calls=[]
    def runner(command,**kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command,1,'',missing)
    with pytest.raises(subprocess.CalledProcessError):
        install(['canonical-installer'],registry,wait_seconds=20,runner=runner,clock=lambda:clock[0],sleep=lambda n:clock.__setitem__(0,clock[0]+n))
    assert clock[0]==20 and len(calls)==3
