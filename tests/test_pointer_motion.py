import pytest
from phonelab.pointer_motion import plan_pointer_step, verify_pointer_step


def test_observed_reversed_axes_drive_toward_target_instead_of_screen_edge():
    step=plan_pointer_step((788.5,1324.5),(40,150),(-1.25,-1.25))
    assert step=={'dx':16,'dy':16}
    verify_pointer_step((788.5,1324.5),(768.5,1304.5),step,(-1.25,-1.25))
    with pytest.raises(ValueError,match='direction'):
        verify_pointer_step((413.5,1344.5),(433.5,1344.5),{'dx':-16,'dy':0},(1.25,1.25))


@pytest.mark.parametrize('gains',[(1.25,1.25),(-1.25,1.25),(1.25,-1.25),(-1.25,-1.25)])
def test_same_goal_works_with_each_device_axis_direction(gains):
    pos=(400.,800.);goal=(80.,110.)
    for _ in range(100):
        step=plan_pointer_step(pos,goal,gains)
        if step=={'dx':0,'dy':0}:break
        after=(pos[0]+step['dx']*gains[0],pos[1]+step['dy']*gains[1])
        verify_pointer_step(pos,after,step,gains);pos=after
    assert max(abs(pos[i]-goal[i]) for i in (0,1))<=6


@pytest.mark.parametrize('gains',[None,(0,1),(float('nan'),1),(True,1),(1,5)])
def test_missing_or_unbounded_calibration_cannot_plan(gains):
    with pytest.raises(ValueError):plan_pointer_step((100,100),(40,150),gains)
