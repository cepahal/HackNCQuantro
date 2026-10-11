from types import SimpleNamespace as N
from backend.services.resolution.sentence_context import reconstruct_sentences, select_context


def row(start,end,text): return N(start=start,end=end,text=text)
def analysis(rows,first=20,last=30,duration=90):
    return N(source_transcript=N(sentences=rows), matches=[N(source_start=first,source_end=last)], source=N(duration=duration))


def test_reconstruct_across_caption_blocks_and_abbreviations():
    s = reconstruct_sentences([row(0,2,'Dr. Smith explained'),row(2,4,'the problem.'),row(4,6,'The next point'),row(6,8,'is important!'),row(8,10,'unfinished fragment')])
    assert len(s)==2
    assert s[0]=={'text':'Dr. Smith explained the problem.','start':0,'end':4}
    assert s[1]['end']==8


def test_selected_one_to_three_adjacent_sentences():
    a = analysis([row(0,5,'First complete thought.'),row(5,10,'Second complete thought.'),row(10,20,'Third complete thought.'),row(20,30,'The matched passage.'),row(30,40,'First following thought.'),row(40,50,'Second following thought.'),row(50,60,'Third following thought.')])
    r=select_context(a,chooser=lambda *args:(3,1))
    assert r['start_seconds']==0 and r['end_seconds']==40
    assert len(r['before'])==3 and len(r['after'])==1


def test_bad_selector_uses_two_sentences():
    a=analysis([row(0,10,'One previous thought.'),row(10,20,'Another previous thought.'),row(30,40,'One subsequent thought.'),row(40,50,'Another subsequent thought.')])
    r=select_context(a,chooser=lambda *args:(-1,99))
    assert r['strategy']=='nearest_two_sentences'
    assert r['start_seconds']==0 and r['end_seconds']==50


def test_unpunctuated_captions_fall_back_per_side():
    a=analysis([row(0,20,'these captions do not have punctuation'),row(30,40,'these also lack punctuation')])
    # Without punctuation, caption blocks cannot be assumed to be full sentences.
    r=select_context(a,chooser=lambda *args:None)
    assert r['before_method']=='five_second_fallback'
    assert r['start_seconds']==15


def test_silent_missing_transcript_and_source_boundaries():
    r=select_context(analysis([],first=2,last=88),chooser=lambda *args:None)
    assert r['start_seconds']==0 and r['end_seconds']==90
    assert r['before_method']==r['after_method']=='five_second_fallback'


def test_complete_sentence_crossing_clip_boundary():
    a=analysis([row(10,25,'Sentence starts before the clip.'),row(25,38,'Sentence finishes after the clip.')])
    r=select_context(a,chooser=lambda *args:None)
    assert r['start_seconds']==10 and r['end_seconds']==38


def test_unreliable_long_unpunctuated_run_cannot_expand_export():
    a=analysis([row(0,500,'Long caption finally ends.')],first=300,last=310,duration=600)
    r=select_context(a,chooser=lambda *args:None)
    assert r['start_seconds']==295 and r['end_seconds']==315
