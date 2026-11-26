"""Test the new GUI's own callbacks in an isolated, hidden test window."""
import tempfile
import os
from pathlib import Path
import time
import tkinter as tk
import unittest
from unittest.mock import patch
import app
import engine


class AppSmokeTest(unittest.TestCase):
    def test_select_all_changes_pause_without_overwriting_speeds(self):
        root=tk.Tk(); root.withdraw(); gui=app.App(root)
        with patch.object(gui,'save_config'):
            try:
                gui.plan=[engine.PlanRow('甲',[0],120,1.0),engine.PlanRow('乙',[1],80,1.1),engine.PlanRow('丙',[2],220,.9)]
                gui.refresh_table()
                gui.select_all_button.invoke()
                self.assertEqual(len(gui.table.selection()),3)
                self.assertTrue(gui.table.bind('<Control-a>'))
                gui.pause.set(200)
                gui.apply_row()
                root.update_idletasks()
                self.assertEqual([r.pause_ms for r in gui.plan],[200]*3)
                self.assertEqual([r.speed for r in gui.plan],[1.0,1.1,.9])
                self.assertEqual(len(gui.table.selection()),3)
            finally:gui.close()

    def test_batch_speed_preserves_distinct_pauses(self):
        root=tk.Tk(); root.withdraw(); gui=app.App(root)
        with patch.object(gui,'save_config'):
            try:
                gui.plan=[engine.PlanRow('甲',[0],120),engine.PlanRow('乙',[1],80)]
                gui.refresh_table(); gui.select_all()
                gui.speed.set(1.1); gui.apply_row()
                self.assertEqual([r.speed for r in gui.plan],[1.1,1.1])
                self.assertEqual([r.pause_ms for r in gui.plan],[120,80])
            finally:gui.close()

    def test_load_match_render_edit_session_export(self):
        draft_path=os.environ.get('CCJ_TEST_DRAFT')
        script_path=os.environ.get('CCJ_TEST_SCRIPT')
        if not draft_path or not script_path:
            self.skipTest('可选集成测试需要 CCJ_TEST_DRAFT 与 CCJ_TEST_SCRIPT')
        draft=Path(draft_path)
        script_text=Path(script_path).read_text(encoding='utf-8-sig')
        expected_count=len([s for s in script_text.splitlines() if s.strip()])
        root=tk.Tk()
        root.withdraw()
        gui=app.App(root)
        errors=[]
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(app.messagebox,'showwarning',side_effect=lambda *a,**k:errors.append(a)), \
             patch.object(app.messagebox,'showinfo'), \
             patch.object(gui,'save_config'), \
             patch.object(app.filedialog,'askdirectory',return_value=folder):
            try:
                gui.draft.set(str(draft))
                gui.set_script(script_text)
                gui.load_draft()
                self.wait(root,gui)
                self.assertIsNotNone(gui.candidate)
                self.assertTrue(gui.match())
                self.assertEqual(len(gui.plan),expected_count)
                gui.generate()
                self.wait(root,gui)
                self.assertIsNotNone(gui.result,errors)
                original_length=len(gui.result.raw)
                gui.table.selection_set('0')
                gui.pause.set(320)
                gui.speed.set(1)
                gui.apply_row()
                self.assertIsNone(gui.result)
                self.assertEqual(gui.plan[0].pause_ms,320)
                gui.generate()
                self.wait(root,gui)
                self.assertGreater(len(gui.result.raw),original_length)
                gui.export()
                self.assertTrue((gui.last_export/'字幕-连续显示.srt').is_file())
                session=Path(folder)/'方案.json'
                with patch.object(app.filedialog,'asksaveasfilename',return_value=str(session)):
                    gui.save_session()
                with patch.object(app.filedialog,'askopenfilename',return_value=str(session)):
                    gui.open_session()
                self.wait(root,gui)
                self.assertEqual(gui.plan[0].pause_ms,320)
                self.assertEqual(len(gui.plan),expected_count)
                self.assertFalse(errors,errors)
            finally:
                gui.close()

    def wait(self,root,gui):
        until=time.monotonic()+45
        while gui.busy and time.monotonic()<until:
            root.update()
            time.sleep(.01)
        self.assertFalse(gui.busy,'后台任务没有完成')
        root.update()


if __name__=='__main__':unittest.main()
