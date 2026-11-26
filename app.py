from __future__ import annotations
import argparse
import bisect
import dataclasses
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import traceback
import winsound

import engine

APP_TITLE = '配音与字幕整理 v1.1'
STATE_DIR = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'CCJAudioAlign'


class App:
    def __init__(self, root, draft='', script_file=''):
        self.root = root
        root.title(APP_TITLE)
        width=min(1280,max(1080,root.winfo_screenwidth()-80))
        height=min(860,max(740,root.winfo_screenheight()-100))
        root.geometry(f'{width}x{height}+{max(0,(root.winfo_screenwidth()-width)//2)}+{max(0,(root.winfo_screenheight()-height)//2-20)}')
        root.minsize(1080, 740)
        root.configure(bg='#eef2f6')
        self.candidates, self.candidate, self.plan, self.result = [], None, [], None
        self.renderer = engine.Renderer()
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.temp = tempfile.TemporaryDirectory(prefix='CCJAudioAlign_')
        self.play_origin = None
        self.play_mode = ''
        self.last_export = None
        self.loading_text = False
        self.pending_session = None
        self._selection_signature = None
        self._loading_row = False
        self._pause_dirty = False
        self._speed_dirty = False
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.config = engine.read_json(STATE_DIR/'settings.json') or {}
        self.draft = tk.StringVar(value=draft)
        self.ffmpeg = tk.StringVar(value=self.config.get('ffmpeg') or engine.locate_ffmpeg())
        self.output = tk.StringVar(value=self.config.get('output') or str(Path.home()/'Documents'/'配音字幕输出'))
        self.pause = tk.IntVar(value=120)
        self.speed = tk.DoubleVar(value=1.0)
        self.pause.trace_add('write',lambda *args:self.mark_edit('pause'))
        self.speed.trace_add('write',lambda *args:self.mark_edit('speed'))
        self.inner_gap = tk.IntVar(value=80)
        self.status = tk.StringVar(value='先选择剪映草稿，再粘贴逐行文案。')
        self.notice = tk.StringVar(value='每行一条字幕 · 清理首尾静音 · 保留句内停顿 · 字幕连续显示')
        self.current_caption = tk.StringVar(value='生成后，这里会跟随试听显示字幕')
        self.summary = tk.StringVar(value='尚未生成试听')
        self.configure_style()
        self.build_ui()
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self._poll_after_id=self.root.after(100, self.poll)
        self._tick_after_id=self.root.after(80, self.tick_playback)
        self._startup_after_id=None
        if script_file:
            try:
                self.set_script(Path(script_file).read_text(encoding='utf-8-sig'))
            except OSError as e:
                self.status.set(str(e))
        if draft:
            self._startup_after_id=root.after(200, self.load_draft)

    def configure_style(self):
        style = ttk.Style()
        style.theme_use('clam')
        self.root.option_add('*Font', ('Microsoft YaHei UI', 10))
        style.configure('.', font=('Microsoft YaHei UI', 10), background='#eef2f6', foreground='#233247')
        style.configure('TFrame', background='#eef2f6')
        style.configure('Card.TFrame', background='white')
        style.configure('Card.TLabel', background='white')
        style.configure('Muted.TLabel', foreground='#67758a')
        style.configure('TButton', padding=(12, 8), borderwidth=0, background='#e0e7ef')
        style.map('TButton', background=[('active', '#ccd8e5')])
        style.configure('Primary.TButton', background='#176dce', foreground='white')
        style.map('Primary.TButton', background=[('disabled', '#a5bad2'), ('active', '#1058ad')], foreground=[('disabled', '#e8eef6')])
        style.configure('Treeview', background='white', fieldbackground='white', rowheight=31, borderwidth=0, font=('Microsoft YaHei UI', 10))
        style.configure('Treeview.Heading', background='#e9eff6', font=('Microsoft YaHei UI', 10, 'bold'), padding=(7, 8))
        style.map('Treeview', background=[('selected', '#d7eaff')], foreground=[('selected', '#123d71')])
        style.configure('TProgressbar', background='#176dce', troughcolor='#e0e7ef', borderwidth=0)

    def build_ui(self):
        header = tk.Frame(self.root, bg='#172b43', padx=24, pady=17)
        header.pack(fill='x')
        tk.Label(header, text='配音与字幕整理', font=('Microsoft YaHei UI', 21, 'bold'), bg='#172b43', fg='white').pack(side='left')
        tk.Label(header, text='按你的文案断句，让配音与字幕一起顺下来', bg='#172b43', fg='#b6cce3', padx=24).pack(side='left', pady=5)
        ttk.Button(header, text='使用说明', command=self.help).pack(side='right')
        body = ttk.Frame(self.root, padding=(20, 14, 20, 12))
        body.pack(fill='both', expand=True)
        body.columnconfigure(0,weight=1)
        body.rowconfigure(3,weight=1)
        source = ttk.Frame(body)
        source.grid(row=0,column=0,sticky='ew')
        self.pick_button = ttk.Button(source, text='01  选择剪映草稿', style='Primary.TButton', command=self.pick_draft)
        self.pick_button.pack(side='left')
        self.reload_button = ttk.Button(source, text='重新读取', command=self.load_draft)
        self.reload_button.pack(side='left', padx=8)
        ttk.Entry(source, textvariable=self.draft, state='readonly').pack(side='left', fill='x', expand=True, padx=(0, 10))
        self.settings_button = ttk.Button(source, text='设置', command=self.settings)
        self.settings_button.pack(side='right')
        group = ttk.Frame(body)
        group.grid(row=1,column=0,sticky='ew',pady=(9,5))
        ttk.Label(group, text='配音组').pack(side='left', padx=(0, 10))
        self.groups = ttk.Combobox(group, state='readonly')
        self.groups.pack(side='left', fill='x', expand=True)
        self.groups.bind('<<ComboboxSelected>>', self.choose_candidate)
        ttk.Label(body, textvariable=self.notice, foreground='#846123', wraplength=1200).grid(row=2,column=0,sticky='ew',pady=(3,10))
        pane = ttk.Panedwindow(body, orient='horizontal')
        pane.grid(row=3,column=0,sticky='nsew')
        left = ttk.Frame(pane, style='Card.TFrame', padding=14)
        right = ttk.Frame(pane, style='Card.TFrame', padding=14)
        pane.add(left, weight=2)
        pane.add(right, weight=4)
        left.columnconfigure(0,weight=1)
        left.rowconfigure(2,weight=1)
        right.columnconfigure(0,weight=1)
        right.rowconfigure(1,weight=1)
        ttk.Label(left, text='02  逐行文案', style='Card.TLabel', font=('Microsoft YaHei UI', 12, 'bold')).grid(row=0,column=0,sticky='w')
        ttk.Label(left, text='一行一条字幕，保留你输入的文字', style='Card.TLabel', foreground='#67758a').grid(row=1,column=0,sticky='w',pady=(3,9))
        editor = ttk.Frame(left, style='Card.TFrame')
        editor.grid(row=2,column=0,sticky='nsew')
        self.script = tk.Text(editor, wrap='word', undo=True, relief='flat', bg='#f7f9fc', fg='#24364b',
                              insertbackground='#176dce', padx=12, pady=10, spacing1=3, spacing3=4,
                              font=('Microsoft YaHei UI', 11), width=31,height=6)
        scroll = ttk.Scrollbar(editor, command=self.script.yview)
        self.script.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.script.pack(fill='both', expand=True)
        self.script.bind('<<Modified>>', self.script_changed)
        tools = ttk.Frame(left, style='Card.TFrame')
        tools.grid(row=3,column=0,sticky='ew',pady=(9,0))
        self.import_button = ttk.Button(tools, text='导入 TXT', command=self.import_text)
        self.import_button.pack(side='left')
        self.clean_button = ttk.Button(tools, text='去掉标点', command=self.clean_punctuation)
        self.clean_button.pack(side='left', padx=6)
        self.source_text_button = ttk.Button(tools, text='用草稿文案', command=self.use_draft_text)
        self.source_text_button.pack(side='left')
        self.match_button = ttk.Button(left, text='按这份文案匹配配音 →', style='Primary.TButton', command=self.match)
        self.match_button.grid(row=4,column=0,sticky='ew',pady=(9,0))
        title = ttk.Frame(right, style='Card.TFrame')
        title.grid(row=0,column=0,sticky='ew')
        ttk.Label(title, text='03  逐句试听与节奏', style='Card.TLabel', font=('Microsoft YaHei UI', 12, 'bold')).pack(side='left')
        ttk.Label(title, textvariable=self.summary, style='Card.TLabel', foreground='#67758a').pack(side='right')
        self.select_all_button = ttk.Button(title, text='全选 (Ctrl+A)', command=self.select_all)
        self.select_all_button.pack(side='right',padx=(6,10))
        grid = ttk.Frame(right, style='Card.TFrame')
        grid.grid(row=1,column=0,sticky='nsew',pady=(10,8))
        cols = ('number', 'text', 'clips', 'pause', 'speed', 'start')
        self.table = ttk.Treeview(grid, columns=cols, show='headings', selectmode='extended', height=6)
        headings = ('序号', '字幕文案', '配音段', '句后停顿', '语速', '开始时间')
        widths = (42, 280, 55, 82, 62, 80)
        for c, h, w in zip(cols, headings, widths):
            self.table.heading(c, text=h)
            self.table.column(c, width=w, minwidth=40, stretch=c=='text', anchor='w' if c=='text' else 'center')
        sb = ttk.Scrollbar(grid, command=self.table.yview)
        self.table.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.table.pack(fill='both', expand=True)
        self.table.bind('<<TreeviewSelect>>', self.row_selected)
        self.table.bind('<Double-1>', lambda e:self.play_line())
        self.table.bind('<Control-a>',self.select_all)
        self.table.bind('<Control-A>',self.select_all)
        edit = ttk.Frame(right, style='Card.TFrame')
        edit.grid(row=2,column=0,sticky='ew')
        ttk.Label(edit, text='句后停顿', style='Card.TLabel').pack(side='left')
        self.pause_box = ttk.Spinbox(edit, from_=0, to=3000, increment=20, textvariable=self.pause, width=6)
        self.pause_box.pack(side='left', padx=5)
        ttk.Label(edit, text='毫秒   语速', style='Card.TLabel').pack(side='left')
        self.speed_box = ttk.Spinbox(edit, from_=.75, to=1.5, increment=.05, format='%.2f', textvariable=self.speed, width=5)
        self.speed_box.pack(side='left', padx=5)
        self.apply_button = ttk.Button(edit, text='应用到选中句', command=self.apply_row)
        self.apply_button.pack(side='left', padx=(5, 0))
        self.natural_button = ttk.Button(edit, text='恢复自然停顿', command=self.reset_pauses)
        self.natural_button.pack(side='right')
        ttk.Label(right, text='支持全选或多选，只应用改动的项目；100 毫秒 = 0.1 秒。末句不加尾停顿。',
                  style='Card.TLabel', foreground='#67758a').grid(row=3,column=0,sticky='w',pady=(7,9))
        caption_frame = tk.Frame(right, bg='#172b43', height=60)
        caption_frame.grid(row=4,column=0,sticky='ew')
        tk.Label(caption_frame, textvariable=self.current_caption, wraplength=620,
                 font=('Microsoft YaHei UI', 12), fg='white', bg='#172b43', pady=14, padx=10).pack(fill='x')
        controls = ttk.Frame(body)
        controls.grid(row=4,column=0,sticky='ew',pady=(12,8))
        self.generate_button = ttk.Button(controls, text='生成 / 更新试听', style='Primary.TButton', command=self.generate)
        self.generate_button.pack(side='left')
        self.play_all_button = ttk.Button(controls, text='▶ 整段试听', command=self.play_all, state='disabled')
        self.play_all_button.pack(side='left', padx=(8, 4))
        self.play_line_button = ttk.Button(controls, text='▶ 选中句', command=self.play_line, state='disabled')
        self.play_line_button.pack(side='left', padx=4)
        self.play_join_button = ttk.Button(controls, text='▶ 两句衔接', command=lambda:self.play_line(True), state='disabled')
        self.play_join_button.pack(side='left', padx=4)
        ttk.Button(controls, text='■ 停止', command=self.stop).pack(side='left', padx=4)
        self.export_button = ttk.Button(controls, text='导出 WAV + 连续字幕', style='Primary.TButton', command=self.export, state='disabled')
        self.export_button.pack(side='right')
        session = ttk.Frame(body)
        session.grid(row=5,column=0,sticky='ew')
        self.open_session_button = ttk.Button(session, text='打开方案', command=self.open_session)
        self.open_session_button.pack(side='left')
        self.save_session_button = ttk.Button(session, text='保存方案', command=self.save_session)
        self.save_session_button.pack(side='left', padx=5)
        self.open_output_button = ttk.Button(session, text='打开输出文件夹', command=self.open_output)
        self.open_output_button.pack(side='right')
        self.progress = ttk.Progressbar(body, maximum=100)
        self.progress.grid(row=6,column=0,sticky='ew',pady=(10,6))
        ttk.Label(body, textvariable=self.status, foreground='#3f5974', wraplength=1210).grid(row=7,column=0,sticky='ew')

    def save_config(self):
        (STATE_DIR/'settings.json').write_text(json.dumps({'draft': self.draft.get(), 'ffmpeg': self.ffmpeg.get(),
                                                         'output': self.output.get()}, ensure_ascii=False, indent=2), encoding='utf-8')

    def set_script(self, text):
        self.loading_text = True
        self.script.delete('1.0', 'end')
        self.script.insert('1.0', text.strip())
        self.script.edit_modified(False)
        self.loading_text = False

    def script_changed(self, event=None):
        if self.script.edit_modified():
            self.script.edit_modified(False)
            if not self.loading_text:
                self.plan = []
                self.table.delete(*self.table.get_children())
                self.invalidate('文案已修改，请重新匹配。')

    def invalidate(self, status='设置已修改，请更新试听后导出。'):
        self.stop()
        self.result = None
        self.summary.set('待生成试听')
        for b in (self.play_all_button,self.play_line_button,self.play_join_button,self.export_button):
            b.configure(state='disabled')
        self.status.set(status)

    def pick_draft(self):
        previous = self.draft.get() or self.config.get('draft', '')
        initial = str(Path(previous).parent) if previous and Path(previous).parent.is_dir() else str(Path.home()/'Documents')
        folder = filedialog.askdirectory(title='选择单个剪映草稿文件夹', initialdir=initial if Path(initial).is_dir() else None)
        if folder:
            self.draft.set(folder)
            self.load_draft()

    def run_job(self, kind, function):
        if self.busy:
            return
        self.stop()
        self.busy = True
        self.cancel.clear()
        self.script.configure(state='disabled')
        self.groups.configure(state='disabled')
        for b in (self.pick_button,self.reload_button,self.settings_button,self.match_button,self.import_button,self.clean_button,
                  self.apply_button,self.natural_button,self.generate_button,self.save_session_button,self.open_session_button,self.source_text_button,self.select_all_button,
                  self.export_button,self.play_all_button,self.play_line_button,self.play_join_button):
            b.configure(state='disabled')
        def worker():
            try:
                self.events.put(('done',kind,function()))
            except Exception as e:
                if not isinstance(e, engine.UserError):
                    (STATE_DIR/'error.log').write_text(traceback.format_exc(),encoding='utf-8')
                self.events.put(('error',kind,str(e)))
        threading.Thread(target=worker,daemon=True).start()

    def load_draft(self):
        if not self.draft.get():
            return self.pick_draft()
        self.invalidate('正在读取所选草稿…')
        folder = self.draft.get()
        self.run_job('load', lambda:engine.discover(folder))

    def choose_candidate(self, event=None):
        if self.busy:
            return
        idx = self.groups.current()
        if idx < 0:
            return
        self.candidate = self.candidates[idx]
        self.plan = []
        self.table.delete(*self.table.get_children())
        self.invalidate('请核对配音组文案，按你的节奏调整换行后匹配。')
        if not self.script.get('1.0','end').strip():
            self.set_script('\n'.join(c.text for c in self.candidate.clips))
        warning = self.candidate.warnings
        if warning:
            self.notice.set('请核对：'+'；'.join(warning[:2]))
        else:
            self.notice.set('已找到完整独立朗读配音。左侧换行决定最终字幕分段。')

    def import_text(self):
        path = filedialog.askopenfilename(title='导入逐行文案',filetypes=[('文案','*.txt'),('所有文件','*.*')])
        if path:
            try:
                text = Path(path).read_text(encoding='utf-8-sig')
            except UnicodeError:
                text = Path(path).read_text(encoding='gb18030')
            self.set_script(text)
            self.plan = []
            self.table.delete(*self.table.get_children())
            self.invalidate('文案已导入，请匹配。')

    def clean_punctuation(self):
        self.set_script(engine.remove_punctuation(self.script.get('1.0','end')))
        self.plan = []
        self.table.delete(*self.table.get_children())
        self.invalidate('已去掉标点，请核对文字和换行。')

    def use_draft_text(self):
        if self.candidate:
            self.set_script('\n'.join(c.text for c in self.candidate.clips))
            self.plan=[]
            self.table.delete(*self.table.get_children())
            self.invalidate('已载入草稿文案，可按你的节奏修改换行，再匹配。')

    def match(self):
        if self.busy:
            return False
        if not self.candidate:
            messagebox.showinfo(APP_TITLE,'请先选择并读取剪映草稿。')
            return False
        try:
            new = engine.make_plan(self.candidate,self.script.get('1.0','end'))
            old = {r.text:r for r in self.plan}
            for row in new:
                if row.text in old:
                    row.pause_ms, row.speed = old[row.text].pause_ms, old[row.text].speed
            self.plan = new
            self.invalidate(f'匹配成功：{len(self.candidate.clips)} 段朗读 → {len(new)} 条字幕。可以生成试听。')
            self.refresh_table()
            return True
        except engine.UserError as e:
            messagebox.showwarning('需要核对',str(e))
            return False

    def refresh_table(self):
        selection = set(self.table.selection())
        self._selection_signature = None
        self.table.delete(*self.table.get_children())
        for i, row in enumerate(self.plan):
            start = f'{self.result.bounds[i][0]/engine.RATE:.2f}s' if self.result else '—'
            self.table.insert('','end',iid=str(i),values=(i+1,row.text,len(row.pieces),f'{row.pause_ms} ms',f'{row.speed:.2f}×',start))
        valid = [s for s in selection if self.table.exists(s)]
        if valid:
            self.table.selection_set(valid)
        elif self.plan:
            self.table.selection_set('0')
        self.row_selected()
        self.summary.set(f'{len(self.plan)} 条字幕' + (f' · {len(self.result.raw)/2/engine.RATE:.2f} 秒' if self.result else ' · 待试听'))

    def row_selected(self,event=None):
        rows = self.table.selection()
        if tuple(rows)==self._selection_signature:
            return
        self._selection_signature=tuple(rows)
        if rows:
            row=self.plan[int(rows[0])]
            self._loading_row=True
            try:
                self.pause.set(row.pause_ms)
                self.speed.set(row.speed)
            finally:
                self._loading_row=False
            self._pause_dirty=self._speed_dirty=False

    def mark_edit(self,field):
        if not self._loading_row:
            if field=='pause':self._pause_dirty=True
            if field=='speed':self._speed_dirty=True

    def select_all(self,event=None):
        if self.busy:
            return 'break'
        self.stop()
        items=self.table.get_children()
        if items:
            self.table.selection_set(items)
            self.table.focus_set()
            self.row_selected()
            self.status.set(f'已全选 {len(items)} 句。修改句后停顿，再点“应用到选中句”，即可统一设置。')
        return 'break'

    def apply_row(self):
        selected = self.table.selection()
        if not selected:
            return
        if not self._pause_dirty and not self._speed_dirty:
            self.status.set('请先修改句后停顿或语速，再点“应用到选中句”。')
            return
        try:
            pause = self.pause.get() if self._pause_dirty else None
            speed = self.speed.get() if self._speed_dirty else None
            if (pause is not None and not 0<=pause<=3000) or (speed is not None and not .75<=speed<=1.5):
                raise ValueError()
        except (ValueError, tk.TclError):
            return messagebox.showwarning(APP_TITLE,'停顿范围为 0～3000 毫秒，语速范围为 0.75～1.50 倍。')
        for i in selected:
            if pause is not None:self.plan[int(i)].pause_ms=pause
            if speed is not None:self.plan[int(i)].speed=speed
        self.invalidate()
        self.refresh_table()
        fields='和'.join(x for x,value in (('句后停顿',pause),('语速',speed)) if value is not None)
        self.status.set(f'已更新 {len(selected)} 句的{fields}，请点击“生成 / 更新试听”。')

    def reset_pauses(self):
        for row in self.plan:
            row.pause_ms=engine.natural_pause(row.text)
        self.invalidate('已恢复自然停顿；语速保留当前设置。请更新试听。')
        self.refresh_table()

    def generate(self):
        if not self.plan and not self.match():
            return
        if not self.ffmpeg.get():
            messagebox.showinfo(APP_TITLE,'未找到音频工具，请在设置里选择 ffmpeg.exe。')
            return self.settings()
        snapshot = [dataclasses.replace(r,pieces=list(r.pieces)) for r in self.plan]
        candidate, ffmpeg, inner = self.candidate,self.ffmpeg.get(),self.inner_gap.get()
        self.progress['value']=0
        self.status.set('正在清理首尾静音并拼接配音…')
        def progress(done,total,text):
            self.events.put(('progress',done/total*100,f'{done}/{total}  {text}'))
        self.run_job('render',lambda:self.renderer.render(candidate,snapshot,ffmpeg,inner,progress,self.cancel))

    def poll(self):
        try:
            while True:
                event=self.events.get_nowait()
                if event[0]=='progress':
                    self.progress['value']=event[1]
                    self.status.set(event[2])
                    continue
                self.busy=False
                self.script.configure(state='normal')
                self.groups.configure(state='readonly')
                for b in (self.pick_button,self.reload_button,self.settings_button,self.match_button,self.import_button,self.clean_button,
                          self.apply_button,self.natural_button,self.generate_button,self.save_session_button,self.open_session_button,self.source_text_button,self.select_all_button):
                    b.configure(state='normal')
                if event[0]=='error':
                    self.status.set('未完成：'+event[2].splitlines()[0])
                    if event[2]!='已停止生成。':
                        messagebox.showwarning('需要处理',event[2])
                    continue
                if event[1]=='load':
                    self.candidates, unreadable=event[2]
                    self.groups['values']=[f'配音组 {i+1}  |  {len(c.clips)} 段  |  {" / ".join(x.text for x in c.clips[:2])[:45]}' for i,c in enumerate(self.candidates)]
                    self.groups.current(0)
                    self.choose_candidate()
                    if unreadable:
                        self.notice.set('主草稿部分数据无法直接读取，已找到可读复合片段。请核对配音组和左侧文案是否属于本次制作。')
                    if self.pending_session:
                        data=self.pending_session
                        self.pending_session=None
                        idx=next((i for i,c in enumerate(self.candidates) if c.fingerprint==data.get('candidate_fingerprint')),-1)
                        if idx<0:
                            messagebox.showwarning(APP_TITLE,'草稿配音组已变化，原方案未自动套用。请核对后重新匹配。')
                        else:
                            self.groups.current(idx)
                            self.choose_candidate()
                            self.set_script(data['script'])
                            if self.match():
                                saved=data.get('rows',[])
                                if len(saved)==len(self.plan):
                                    for row, settings in zip(self.plan,saved):
                                        if row.text==settings.get('text'):
                                            row.pause_ms=int(settings.get('pause_ms',row.pause_ms))
                                            row.speed=float(settings.get('speed',1))
                                    self.refresh_table()
                    self.save_config()
                elif event[1]=='render':
                    self.result=event[2]
                    self.progress['value']=100
                    self.refresh_table()
                    for b in (self.play_all_button,self.play_line_button,self.play_join_button,self.export_button):
                        b.configure(state='normal')
                    self.current_caption.set('试听已就绪，点击整段或选中一句播放')
                    self.status.set(f'已生成 {len(self.plan)} 条连续字幕 · 配音 {len(self.result.raw)/2/engine.RATE:.2f} 秒。可试听并导出。')
        except queue.Empty:
            pass
        self._poll_after_id=self.root.after(100,self.poll)

    def play_audio(self,raw,mode,origin=0):
        self.stop()
        path=Path(self.temp.name)/'试听.wav'
        engine.write_wav(path,raw)
        try:
            winsound.PlaySound(str(path),winsound.SND_FILENAME|winsound.SND_ASYNC|winsound.SND_NODEFAULT)
        except RuntimeError as e:
            return messagebox.showwarning(APP_TITLE,'音频设备无法播放：'+str(e))
        self.play_origin=time.perf_counter()-origin
        self.play_duration=len(raw)/2/engine.RATE+origin
        self.play_mode=mode

    def play_all(self):
        if self.result:
            self.play_audio(self.result.raw,'all')

    def play_line(self,join=False):
        if not self.result or self.busy:
            return
        selected=self.table.selection()
        i=int(selected[0]) if selected else 0
        raw=self.result.lines[i]
        if join and i+1<len(self.result.lines):
            gap=round(self.result.plan[i].pause_ms*engine.RATE/1000)
            raw+=b'\0\0'*gap+self.result.lines[i+1]
        self.play_audio(raw,'line')
        self.current_caption.set(self.result.plan[i].text + ('  →  '+self.result.plan[i+1].text if join and i+1<len(self.plan) else ''))

    def tick_playback(self):
        if self.play_origin is not None:
            elapsed=time.perf_counter()-self.play_origin
            if elapsed>=self.play_duration:
                self.stop()
            elif self.play_mode=='all' and self.result:
                starts=[b[0]/engine.RATE for b in self.result.bounds]
                i=max(0,bisect.bisect_right(starts,elapsed)-1)
                self.current_caption.set(self.result.plan[i].text)
                self.table.see(str(i))
                self.table.selection_set(str(i))
        self._tick_after_id=self.root.after(80,self.tick_playback)

    def stop(self):
        winsound.PlaySound(None,0)
        self.play_origin=None
        if self.busy:
            self.cancel.set()

    def export(self):
        if not self.result:
            return
        folder=filedialog.askdirectory(title='选择输出位置（每次会新建结果文件夹）',initialdir=self.output.get() if Path(self.output.get()).is_dir() else None)
        if not folder:
            return
        try:
            self.last_export=engine.export_result(self.result,folder)
            self.output.set(folder)
            self.save_config()
            self.status.set('已导出：'+str(self.last_export))
            messagebox.showinfo('导出完成',f'配音.wav\n字幕-连续显示.srt\n逐行文案.txt\n处理记录.json\n\n保存到：\n{self.last_export}')
        except OSError as e:
            messagebox.showerror(APP_TITLE,str(e))

    def open_output(self):
        folder=self.last_export or Path(self.output.get())
        if folder.is_dir():
            os.startfile(str(folder))
        else:
            messagebox.showinfo(APP_TITLE,'尚未导出结果。')

    def save_session(self):
        if not self.candidate:
            return messagebox.showinfo(APP_TITLE,'请先选择草稿。')
        path=filedialog.asksaveasfilename(title='保存整理方案',defaultextension='.json',filetypes=[('整理方案','*.json')])
        if path:
            data={'kind':'CCJAudioAlignSession','version':1,'draft':self.draft.get(),
                  'candidate_fingerprint':self.candidate.fingerprint,'script':self.script.get('1.0','end').strip(),
                  'rows':[dataclasses.asdict(r) for r in self.plan],'inner_gap_ms':self.inner_gap.get()}
            try:
                Path(path).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
                self.status.set('方案已保存，包含文案分行、逐句停顿和语速。')
            except OSError as e:
                messagebox.showerror(APP_TITLE,str(e))

    def open_session(self):
        path=filedialog.askopenfilename(title='打开整理方案',filetypes=[('整理方案','*.json')])
        if path:
            data=engine.read_json(path)
            if not isinstance(data,dict) or data.get('kind')!='CCJAudioAlignSession' or not isinstance(data.get('script'),str) or not isinstance(data.get('draft'),str):
                return messagebox.showwarning(APP_TITLE,'这不是有效的整理方案。')
            if not Path(data['draft']).is_dir():
                return messagebox.showwarning(APP_TITLE,'原草稿文件夹已移动。请重新选择草稿、导入文案后匹配。')
            self.pending_session=data
            self.set_script(data['script'])
            self.draft.set(data['draft'])
            self.inner_gap.set(int(data.get('inner_gap_ms',80)))
            self.load_draft()

    def settings(self):
        win=tk.Toplevel(self.root)
        win.title('设置')
        win.geometry('670x250')
        win.transient(self.root)
        win.grab_set()
        frame=ttk.Frame(win,padding=20)
        frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='音频工具 ffmpeg.exe（优先自动使用已安装的剪映）').pack(anchor='w')
        row=ttk.Frame(frame)
        row.pack(fill='x',pady=9)
        ff=tk.StringVar(value=self.ffmpeg.get())
        ttk.Entry(row,textvariable=ff).pack(side='left',fill='x',expand=True)
        def browse():
            path=filedialog.askopenfilename(parent=win,title='选择 ffmpeg.exe',filetypes=[('音频工具','ffmpeg.exe')])
            if path:ff.set(path)
        ttk.Button(row,text='选择',command=browse).pack(side='left',padx=7)
        gap=tk.IntVar(value=self.inner_gap.get())
        row2=ttk.Frame(frame)
        row2.pack(fill='x',pady=8)
        ttk.Label(row2,text='同一行由多段配音接成时，接缝停顿').pack(side='left')
        ttk.Spinbox(row2,from_=0,to=1000,increment=20,textvariable=gap,width=6).pack(side='left',padx=7)
        ttk.Label(row2,text='毫秒，默认 80').pack(side='left')
        def save():
            try:
                val=gap.get()
                if not 0<=val<=1000:raise ValueError()
            except (ValueError,tk.TclError):
                return messagebox.showwarning(APP_TITLE,'接缝停顿应为 0～1000 毫秒。',parent=win)
            self.ffmpeg.set(ff.get())
            self.inner_gap.set(val)
            self.save_config()
            self.renderer.cache.clear()
            self.invalidate()
            win.destroy()
        ttk.Button(frame,text='保存设置',style='Primary.TButton',command=save).pack(anchor='e',pady=9)

    def help(self):
        messagebox.showinfo('使用说明',
            '1 选择包含 draft_content.json 的单个剪映草稿文件夹。\n'
            '2 核对配音组，粘贴最终文案；每行就是一条字幕。\n'
            '3 点击匹配，生成试听。点“全选”或在列表中按 Ctrl+A 可统一调整。只应用被改动的停顿或语速。\n'
            '4 试听满意后导出 WAV 和连续显示的 SRT。\n\n'
            '默认只去除每段首尾静音，保留句内停顿和呼吸。字幕在声音停顿时继续显示。\n'
            '允许合并完整朗读片段；若换行落在一段配音中间，会提示先拆分朗读。\n'
            '主稿加密时仅显示可读复合片段，请核对内容；不读取旧备份。\n'
            '所有处理均在本机完成，原始草稿和原始音频保持不变。')

    def close(self):
        self.cancel.set()
        winsound.PlaySound(None,0)
        for callback in (self._poll_after_id,self._tick_after_id,self._startup_after_id):
            if callback:
                try:self.root.after_cancel(callback)
                except tk.TclError:pass
        try:self.save_config()
        except OSError:pass
        try:self.temp.cleanup()
        except OSError:pass
        self.root.update_idletasks()
        self.root.destroy()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--draft',default='')
    parser.add_argument('--script-file',default='')
    parser.add_argument('--self-check-dir',default='',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.self_check_dir:
        output=Path(args.self_check_dir)
        output.mkdir(parents=True,exist_ok=True)
        try:
            candidates,unreadable=engine.discover(args.draft)
            script=Path(args.script_file).read_text(encoding='utf-8-sig')
            matches=[]
            for candidate in candidates:
                try:matches.append((candidate,engine.make_plan(candidate,script)))
                except engine.UserError:pass
            if len(matches)!=1:
                raise engine.UserError('验收文案未唯一匹配到一个配音组。')
            candidate,plan=matches[0]
            result=engine.Renderer().render(candidate,plan,engine.locate_ffmpeg())
            folder=engine.export_result(result,output)
            report={'ok':True,'rows':len(plan),'duration_seconds':len(result.raw)/2/engine.RATE,'folder':str(folder)}
        except Exception as e:
            report={'ok':False,'error':str(e),'traceback':traceback.format_exc()}
        (output/'程序验收.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root=tk.Tk()
    def callback_error(exc,value,tb):
        STATE_DIR.mkdir(parents=True,exist_ok=True)
        (STATE_DIR/'error.log').write_text(''.join(traceback.format_exception(exc,value,tb)),encoding='utf-8')
        messagebox.showerror(APP_TITLE,'操作未完成：'+str(value)+'\n请核对输入。详细信息已写入本地错误日志。')
    root.report_callback_exception=callback_error
    App(root,args.draft,args.script_file)
    root.mainloop()


if __name__=='__main__':
    main()
