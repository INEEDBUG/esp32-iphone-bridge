from phonelab.web import _valid_browser_event


def test_derived_drag_assertion_is_distinct_from_a_trusted_native_drop():
    event={'type':'drag_result','isTrusted':False,'completed':True,'dropCount':1,
           'trustedPointerSequence':True,'dragState':'dropped'}
    assert _valid_browser_event(event)
    for key,value in [('isTrusted',True),('dropCount',True),('dropCount',-1),
                      ('trustedPointerSequence',False),('dragState','secret')]:
        assert not _valid_browser_event({**event,key:value})
