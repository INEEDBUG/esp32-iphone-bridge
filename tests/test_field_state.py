from phonelab.web import _valid_browser_event


def test_derived_field_state_only_allows_synthetic_prefixes_and_consistent_caret():
    value={'type':'field_state','isTrusted':False,'text':'test12','textLength':6,
        'syntheticTextPrefix':True,'focused':True,'caretAtEnd':True,'fromTrustedInput':True}
    assert _valid_browser_event(value)
    for key,bad in [('text','private message'),('textLength',7),('isTrusted',True),
                    ('syntheticTextPrefix',False),('focused','true')]:
        assert not _valid_browser_event({**value,key:bad})
