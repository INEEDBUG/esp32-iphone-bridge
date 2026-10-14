"""Execute extracted sketch functions with a deterministic clock and fake endpoints."""
import os
import re
from pathlib import Path
import subprocess
import pytest
from test_ble_output_safety import host_cpp_compiler

ROOT=Path(__file__).resolve().parents[1]


def function(source,signature):
    match=re.search(re.escape(signature)+r'[^;{]*\{',source)
    assert match is not None,signature
    start=match.start();opening=match.end()-1;depth=1;end=opening+1
    while depth:
        depth+=(source[end]=='{')-(source[end]=='}');end+=1
    return source[start:end]


@pytest.mark.skipif(not host_cpp_compiler(),reason='native C++ compiler unavailable')
def test_actual_sketch_keyboard_pacing_cancellation_and_bounded_idle_release(tmp_path):
    source=(ROOT/'firmware/classic_ble/classic_ble.ino').read_text()
    selected='\n'.join(function(source,x) for x in (
        'void serviceKeyboardRelease()', 'void releaseAll(', 'bool actionCanContinue(',
        'bool waitKeyboardPhase(', 'bool typeText('))
    constants='\n'.join(re.findall(r'constexpr uint(?:32|8)_t (?:SESSION_WATCHDOG_MS|KEYBOARD_PHASE_MS|KEYBOARD_RELEASE_RETRY_MS|MAX_KEYBOARD_RELEASE_ATTEMPTS) = \d+;',source))
    assert len(constants.splitlines())==4
    prelude=r'''
#include <cassert>
#include <cstdint>
#include <string>
#include <vector>
using std::size_t;
uint32_t tick=0, cancelAt=0xffffffff;
uint32_t millis(){return tick;}
void delay(uint32_t ms){tick+=ms;}
struct Request{std::string op="type",text="ab";uint32_t deadline=1000;};
struct Session{bool empty=true;bool isEmpty()const{return empty;}} activeSession;
// Production timing constants are injected before this harness.
uint32_t lastHostActivity=0,mouseFailureAtActionStart=0,keyboardFailureAtActionStart=0;
uint32_t nextKeyboardReleaseAt=0;
uint8_t keyboardReleaseAttemptsRemaining=0;
bool keyboardReleaseEpisodeActive=false;
bool cancelRequested=false,lastBleReady=false;
struct EspBleHidMouseReport{};
struct Mouse{bool sendReport(const EspBleHidMouseReport&){return true;}};
struct Ble{Mouse mouse;void update(){}Mouse& hidMouse(){return mouse;}} ble;
struct Output{
 bool up=true,pending=false,releaseAccepted=false;unsigned retries=0;uint32_t failures=0;
 bool connected()const{return up;}bool mouseReady()const{return up;}
 uint32_t mouseSendFailureCount()const{return 0;}
 uint32_t keyboardSendFailureCount()const{return failures;}
 bool keyboardReleasePending()const{return pending;}
 void requireKeyboardRelease(){pending=true;}
 void discardPendingMouseMotion(){}
 bool retryKeyboardRelease(){++retries;if(releaseAccepted)pending=false;return !pending;}
} hidOutput;
struct Bridge{
 int phases=0;std::vector<uint32_t> writes;
 void typeText(const char*){phases=3;}
 void update(){writes.push_back(tick);--phases;}
 void cancelText(){phases=0;}
 bool typingActive()const{return phases>0;}
} bridge;
namespace esp32keybridge{constexpr size_t kAxisCount=4;enum class Axis{X,Y,Wheel,Pan};}
struct Input{void clear(){}void takeAxisDelta(esp32keybridge::Axis){}} commandInput;
void disarm(const char*,bool){cancelRequested=true;activeSession.empty=true;}
void pollWhileBusy(const Request&){if(tick>=cancelAt)cancelRequested=true;}
bool expired(const Request&r){return tick>=r.deadline;}
'''
    suffix=r'''
int main(){
 Request r;
 assert(typeText(r));assert(bridge.writes.size()==6);
 for(size_t i=1;i<bridge.writes.size();++i)assert(bridge.writes[i]-bridge.writes[i-1]>=30);
 assert(tick==180);
 bridge.writes.clear();tick=0;cancelAt=45;cancelRequested=false;
 assert(!typeText(r));assert(tick==45 && bridge.writes.size()==2);
 bridge.writes.clear();tick=0;cancelAt=0xffffffff;cancelRequested=false;r.deadline=100;
 assert(!typeText(r));assert(tick==100 && bridge.writes.size()==4);
 cancelRequested=false;r.deadline=1000;hidOutput.failures=1;keyboardFailureAtActionStart=0;
 assert(!actionCanContinue(r));assert(cancelRequested);
 cancelRequested=false;hidOutput.failures=0;hidOutput.pending=true;
 assert(!actionCanContinue(r));assert(cancelRequested);
 hidOutput.pending=true;hidOutput.retries=0;keyboardReleaseAttemptsRemaining=10;
 keyboardReleaseEpisodeActive=true;nextKeyboardReleaseAt=0;tick=0;
 // Repeated STOP/disarm requests must not renew a pending episode's budget.
 for(unsigned i=0;i<1000;++i){releaseAll(true);serviceKeyboardRelease();++tick;}
 assert(hidOutput.retries==10 && keyboardReleaseAttemptsRemaining==0 && hidOutput.pending);
 hidOutput.retries=0;keyboardReleaseAttemptsRemaining=10;nextKeyboardReleaseAt=tick;
 hidOutput.releaseAccepted=true;serviceKeyboardRelease();
 assert(!hidOutput.pending && hidOutput.retries==1);
 for(unsigned i=0;i<100;++i){++tick;serviceKeyboardRelease();}
 assert(hidOutput.retries==1);
 hidOutput.pending=true;hidOutput.up=false;keyboardReleaseAttemptsRemaining=10;
 serviceKeyboardRelease();assert(hidOutput.retries==1 && keyboardReleaseAttemptsRemaining==10);
}
'''
    compiler=host_cpp_compiler();cpp=tmp_path/'keyboard_safety.cpp';exe=tmp_path/'keyboard_safety.exe'
    cpp.write_text('#include <cstdint>\n'+constants+'\n'+prelude+selected+suffix)
    env=dict(os.environ);env['PATH']=str(Path(compiler).parent)+os.pathsep+env.get('PATH','')
    compiled=subprocess.run([compiler,'-std=c++11',str(cpp),'-o',str(exe)],capture_output=True,text=True,env=env)
    assert compiled.returncode==0,compiled.stderr
    ran=subprocess.run([str(exe)],capture_output=True,text=True,env=env)
    assert ran.returncode==0,ran.stderr
