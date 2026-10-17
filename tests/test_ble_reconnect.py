"""Execute the actual disconnect callback with a failed or successful advertiser."""
from pathlib import Path
import os,re,shutil,subprocess,tempfile
import pytest
from phonelab.paths import resolve_data_root

ROOT=Path(__file__).resolve().parents[1]


def test_disconnect_reopens_advertising_without_rearming_or_replaying_input():
    compiler=shutil.which('g++') or shutil.which('clang++')
    if not compiler:
        fallback=resolve_data_root()/'airplay-build/msys2/msys64/ucrt64/bin/g++.exe'
        if not fallback.is_file():pytest.skip('host C++ compiler unavailable')
        compiler=str(fallback)
    text=(ROOT/'firmware/classic_ble/classic_ble.ino').read_text(encoding='utf-8')
    match=re.search(r'ble\.onDisconnected\(\[\]\(const EspBleConnection &\w*\)\s*\{(.*?)\n  \}\);',text,re.S)
    assert match is not None
    harness=r'''
#include <cassert>
#include <cstdint>
#include <cstring>
bool armed=true, axesPending=true, queuedKeys=true;
int lastDisconnectReason=0;
uint32_t disconnectCount=0;
bool advertisingRestartFailed=false;
int advertisingStarts=0;
bool advertisingSucceeds=true;
struct EspBleConnection {int disconnectReason;};
struct Advertising {
  bool start() {
    assert(!armed && !axesPending && !queuedKeys);
    ++advertisingStarts; return advertisingSucceeds;
  }
} advertiser;
struct Ble {Advertising &advertising() {return advertiser;}} ble;
void disarm(const char *reason,bool transmit) {
  assert(std::strcmp(reason,"ble_disconnected")==0 && !transmit);
  armed=false;axesPending=false;queuedKeys=false;
}
void disconnected(const EspBleConnection &connection) {
/*__CALLBACK__*/
}
int main(int argc,char**) {
  advertisingSucceeds=argc==1;
  disconnected(EspBleConnection{19});
  assert(advertisingStarts==1);
  assert(!armed && !axesPending && !queuedKeys);
  assert(lastDisconnectReason==19 && disconnectCount==1);
  assert(advertisingRestartFailed==!advertisingSucceeds);
  return 0;
}
'''.replace('/*__CALLBACK__*/',match.group(1))
    with tempfile.TemporaryDirectory(prefix='ble-reconnect-') as temp:
        cpp=Path(temp)/'callback.cpp';exe=Path(temp)/'callback.exe'
        cpp.write_text(harness,encoding='utf-8')
        env=dict(os.environ);env['PATH']=str(Path(compiler).parent)+os.pathsep+env.get('PATH','')
        built=subprocess.run([compiler,'-std=c++17',str(cpp),'-o',str(exe)],capture_output=True,text=True,env=env)
        assert built.returncode==0,built.stderr
        for args in ([],['advertising-fails']):
            result=subprocess.run([str(exe),*args],capture_output=True,text=True,env=env)
            assert result.returncode==0,result.stderr
