import json
from pathlib import Path
import tempfile
import unittest
import array
import engine


class MatchingTests(unittest.TestCase):
    def make_candidate(self,texts):
        return engine.Candidate(Path('draft.json'),'测试',[
            engine.Clip(t,Path(f'{i}.wav'),str(i),i) for i,t in enumerate(texts)],'test',[])

    def test_custom_lines_merge_and_preserve_d3_spelling(self):
        c=self.make_candidate(['关节骨骼问题','也接踵而至','更有钙和维生素D₃'])
        p=engine.make_plan(c,'关节骨骼问题也接踵而至\n更有钙和维生素D3')
        self.assertEqual(p[0].pieces,[0,1])
        self.assertEqual(p[1].text,'更有钙和维生素D3')

    def test_text_mismatch_does_not_guess(self):
        with self.assertRaisesRegex(engine.UserError,'不一致'):
            engine.make_plan(self.make_candidate(['钙质悄悄流失']),'钙质慢慢流失')

    def test_boundary_inside_audio_is_explicit(self):
        with self.assertRaisesRegex(engine.UserError,'配音内部'):
            engine.make_plan(self.make_candidate(['上了年纪']),'上了\n年纪')

    def test_punctuation_requires_explicit_clean(self):
        c=self.make_candidate(['上了年纪'])
        with self.assertRaisesRegex(engine.UserError,'标点'):
            engine.make_plan(c,'上了年纪。')
        self.assertEqual(engine.remove_punctuation('上了年纪。\nD3'),'上了年纪\nD3')

    def test_continuous_subtitle_times(self):
        p=[engine.PlanRow('甲',[0]),engine.PlanRow('乙',[1])]
        text=engine.srt_text(p,[(0,48000),(60000,96000)])
        self.assertIn('00:00:00,000 --> 00:00:01,250',text)
        self.assertIn('00:00:01,250 --> 00:00:02,000',text)

    def test_only_edge_silence_removed(self):
        silence=array.array('h',[0]*4800)
        voice=array.array('h',[5000,-5000]*4800)
        raw=(silence+voice+silence+voice+silence).tobytes()
        clean,head,tail=engine.trim_silence(raw)
        self.assertAlmostEqual(head,.04,places=2)
        self.assertAlmostEqual(tail,.04,places=2)
        self.assertIn(silence.tobytes(),clean)

    def test_renamed_draft_audio_path_is_resolved_locally(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'textReading').mkdir()
            (root/'textReading'/'voice.wav').write_bytes(b'voice')
            actual=engine.resolve_audio(root,'D:/old-name/textReading/voice.wav')
            self.assertEqual(actual,(root/'textReading'/'voice.wav').resolve())

    def test_backups_are_not_used(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'draft_content.json').write_text('encrypted')
            (root/'template.json.bak').write_text(json.dumps({'materials':{'audios':[]}}))
            with self.assertRaisesRegex(engine.UserError,'没有找到可读取'):
                engine.discover(root)


if __name__=='__main__':
    unittest.main()
