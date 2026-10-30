"""Manual panel. Connecting and refreshing are read-only."""
import argparse
import json
import sys
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from core import Game
from errors import READ, ROLLBACK, TARGET_GONE, IssueQueue, classify, recent_lines, summarize_issues
from session_effects import SessionEffects
from runtime_paths import RESOURCE_HOME, log_directory
from ui_theme import configure, Tooltip, BG, CARD

ISSUE_COLORS = {'': '#9aa0ab', 'gray': '#9aa0ab', 'yellow': '#e0b84f', 'red': '#e0565b'}


class DustDialog:
    def __init__(self, root, provider, visible=True):
        self.provider = provider
        self.state = None
        self.state_game = self.last_game = self.last_record = None
        self.window = tk.Toplevel(root)
        if not visible:
            self.window.withdraw()
        self.window.title('意识尘埃数量')
        self.window.geometry('620x330')
        self.window.minsize(570, 300)
        self.window.transient(root)
        frame = ttk.Frame(self.window, padding=20)
        frame.pack(fill='both', expand=True)
        self.current = tk.StringVar(value='当前数量：未读取')
        ttk.Label(frame, textvariable=self.current, style='Card.TLabel').pack(anchor='w')
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=16)
        ttk.Label(row, text='设置数量').pack(side='left')
        self.amount = tk.StringVar(value='0')
        self.amount_input = ttk.Spinbox(row, from_=0, to=2000000000, increment=1, textvariable=self.amount, width=16)
        self.amount_input.pack(side='left', padx=10)
        self.set_button = ttk.Button(row, text='设置意识尘埃', command=self.apply)
        self.set_button.pack(side='right')
        row = ttk.Frame(frame)
        row.pack(fill='x')
        self.undo_button = ttk.Button(row, text='撤销上次设置', command=self.undo)
        self.undo_button.pack(side='left')
        ttk.Button(row, text='读取当前数量', command=self.refresh).pack(side='right')
        self.set_button.state(['disabled'])
        self.undo_button.state(['disabled'])
        ttk.Label(frame, text='输入整数 0–2,000,000,000。单次设置，可能随自动存档保留。\n游戏显示可能到正常获得／消耗尘埃时才刷新，无需反复设置。\n撤销仅限本窗口上次设置；若游戏已改变数量，则保留游戏的新数量。',
                  style='Hint.TLabel', wraplength=530).pack(anchor='w', pady=(14, 6))
        self.message = tk.StringVar(value='读取不会修改数量。')
        ttk.Label(frame, textvariable=self.message, wraplength=530).pack(anchor='w')
        self.refresh(initial=True)

    def game(self):
        game = self.provider()
        if game is None:
            raise RuntimeError('请先连接游戏')
        return game

    def refresh(self, initial=False):
        try:
            game = self.game()
            state = game.dust_state()
            self.state, self.state_game = state, game
            self.current.set(f"当前意识尘埃：{state['amount']:,}")
            if initial:
                self.amount.set(str(state['amount']))
            self.set_button.state(['!disabled'])
            self.message.set('读取完成；游戏 HUD 可能仍显示上次缓存的数字。')
        except Exception as exc:
            self.state = self.state_game = None
            self.set_button.state(['disabled'])
            self.fail(exc)

    def apply(self):
        try:
            game = self.game()
            if not self.state or game is not self.state_game:
                raise RuntimeError('连接已变化，请重新读取当前数量')
            record = game.set_dust(int(self.amount.get()), expected_token=self.state['token'])
            self.last_record, self.last_game = record, game
            self.undo_button.state(['!disabled'])
            self.refresh()
            self.message.set('意识尘埃已设置；游戏数字可能需正常获得／消耗一次后刷新。可撤销上次设置。')
        except Exception as exc:
            self.fail(exc)

    def undo(self):
        try:
            game = self.game()
            if self.last_record is None or game is not self.last_game:
                raise RuntimeError('原连接已变化，不能撤销旧记录')
            game.restore_record(self.last_record)
            self.last_record = self.last_game = None
            self.undo_button.state(['disabled'])
            self.refresh()
            self.message.set('上次设置已撤销；游戏显示可能仍需刷新。')
        except Exception as exc:
            self.fail(exc)


class Panel:
    def __init__(self, root, autoconnect=True):
        self.root, self.game = root, None
        self.controls = []
        root.title('影之刃零 · 修改器 0.13.1')
        root.geometry(f'1040x{min(880, root.winfo_screenheight() - 90)}')
        root.minsize(960, 680)
        root.protocol('WM_DELETE_WINDOW', self.close)
        configure(root)
        icon = RESOURCE_HOME / 'assets' / 'app.ico'
        if icon.is_file():
            root.iconbitmap(default=str(icon))
        outer = ttk.Frame(root, padding=(24, 18))
        outer.pack(fill='both', expand=True)
        header = ttk.Frame(outer)
        header.pack(fill='x', pady=(0, 14))
        title = ttk.Frame(header)
        title.pack(side='left')
        ttk.Label(title, text='影之刃零', style='Title.TLabel').pack(anchor='w')
        ttk.Label(title, text='修改器   /   0.13.1   /   悬停查看功能说明', style='Hint.TLabel').pack(anchor='w', pady=(2, 0))
        self.connect_button = ttk.Button(header, text='连接游戏', command=self.connect, style='Accent.TButton')
        self.connect_button.pack(side='right', padx=(16, 0))
        self.connection = tk.StringVar(value='●  未连接')
        ttk.Label(header, textvariable=self.connection, style='Hint.TLabel').pack(side='right')

        overview = ttk.Frame(outer)
        overview.pack(fill='x', pady=(0, 16))
        self.metrics = {}
        for i, (key, label) in enumerate((('health', '生命'), ('shachi', '杀气'), ('ultimate', '精纯杀气'), ('medicine', '葫芦次数'))):
            overview.columnconfigure(i, weight=1, uniform='metric')
            card = ttk.Frame(overview, style='Card.TFrame', padding=(16, 10))
            card.grid(row=0, column=i, sticky='nsew', padx=(0 if i == 0 else 8, 0))
            ttk.Label(card, text=label, style='Muted.Card.TLabel').pack(anchor='w')
            self.metrics[key] = tk.StringVar(value='—')
            ttk.Label(card, textvariable=self.metrics[key], style='Value.Card.TLabel').pack(anchor='w', pady=(3, 0))
        self.values = tk.StringVar(value='进入可操作角色的场景后连接游戏。')
        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill='both', expand=True)
        main = ttk.Frame(self.tabs)
        self.tabs.add(main, text='功能面板')
        help_page = ttk.Frame(self.tabs, padding=20)
        self.tabs.add(help_page, text='使用说明')
        self.canvas = tk.Canvas(main, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(main, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y', padx=(8, 0))
        self.canvas.pack(side='left', fill='both', expand=True)
        frame = ttk.Frame(self.canvas)
        self.content = frame
        self.content_window = self.canvas.create_window(0, 0, window=frame, anchor='nw')
        self.canvas.bind('<Configure>', lambda event: self.canvas.itemconfigure(self.content_window, width=event.width))
        frame.bind('<Configure>', lambda event: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        root.bind('<MouseWheel>', self.scroll)
        frame.columnconfigure(0, weight=1, uniform='column')
        frame.columnconfigure(1, weight=1, uniform='column')
        left = ttk.Frame(frame)
        right = ttk.Frame(frame)
        left.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        right.grid(row=0, column=1, sticky='nsew', padx=(8, 0))

        self.floor_lock = tk.BooleanVar(value=False)
        self.auto_health = tk.BooleanVar(value=False)
        self.auto_shachi = tk.BooleanVar(value=False)
        self.auto_spirit_shadow = tk.BooleanVar(value=False)
        self.auto_gourd = tk.BooleanVar(value=False)
        self.auto_wine = tk.BooleanVar(value=False)
        self.auto_ultimate = tk.BooleanVar(value=False)
        self.one_hit = tk.BooleanVar(value=False)
        self.teleport_unlocked = tk.BooleanVar(value=False)
        life = self.group(left, '生命保护', '两种保护方式互斥')
        self.switch(life, '强锁血', '维持生命下限；加载时暂缓，新角色就绪后自动继续', self.floor_lock, self.toggle_floor)
        self.switch(life, '自动回血 · 备用', '受伤后补满，无法保证拦截致死伤害', self.auto_health, self.toggle_health)
        self.health_status = tk.StringVar(value='生命保护：关闭')
        ttk.Label(life, textvariable=self.health_status, style='Muted.Card.TLabel', wraplength=400).pack(anchor='w', pady=(7, 2))
        resources = self.group(left, '资源锁定', '消耗后自动补满，不改变容量')
        for label, note, kind, variable in (
                ('锁杀气', '约每 0.2 秒检查', 'shachi', self.auto_shachi),
                ('锁暗源', '影之武装资源', 'spirit_shadow', self.auto_spirit_shadow),
                ('锁葫芦次数', '切换快捷药品可刷新显示', 'Hulu', self.auto_gourd),
                ('锁酒次数', '维持已有酒的使用次数', 'FeiJiu', self.auto_wine),
                ('锁精纯杀气', '主武器绝技资源', 'ultimate', self.auto_ultimate)):
            callback = self.toggle_shachi if kind == 'shachi' else lambda k=kind, v=variable: self.toggle_resource(k, v)
            self.switch(resources, label, note, variable, callback)
        quick = self.group(left, '即时补充', '点击后单次执行')
        for label, callback in (('回满生命', lambda: self.refill('health')),
                                ('补充葫芦', lambda: self.refill('medicine')),
                                ('补满精纯杀气', self.ultimate)):
            button = ttk.Button(quick, text=label, command=callback)
            button.pack(side='left', fill='x', expand=True, padx=(0, 5))
            self.controls.append(button)

        combat = self.group(right, '战斗调整', '攻击 / 承伤 0.1–20 倍')
        self.attack = tk.StringVar(value='2.0')
        self.attack_input = self.number_row(combat, '玩家攻击倍率', '0.1–20 倍', self.attack, 0.1, 20, '应用攻击倍率', lambda: self.modify('attack', self.attack.get()))
        self.incoming = tk.StringVar(value='1.0')
        self.incoming_input = self.number_row(combat, '怪物伤害倍率', '玩家承伤：0.1 减伤 / 20 增伤', self.incoming, 0.1, 20, '应用怪物倍率', lambda: self.modify('incoming', self.incoming.get()))
        self.switch(combat, '一键秒杀', '高攻击 ×10000，与攻击倍率互相替代', self.one_hit, self.toggle_one_hit)
        movement = self.group(right, '移动与切换', '移速 1–5 倍 / 冷却 0.1–2 秒')
        self.movement = tk.StringVar(value='1.5')
        self.movement_input = self.number_row(movement, '移动速度', '1–5 倍 · 冲刺重算后需重新应用', self.movement, 1, 5, '应用移速', lambda: self.modify('movement', self.movement.get()))
        button = ttk.Button(movement, text='恢复移速', command=self.restore_movement)
        button.pack(anchor='e', pady=(0, 8))
        self.controls.append(button)
        self.cooldown = tk.StringVar(value='0.1')
        self.number_row(movement, '武器切换冷却', '0.1–2 秒 · 换装备后需重新应用', self.cooldown, 0.1, 2, '应用切换冷却', lambda: self.modify('cooldown', self.cooldown.get()))
        self.switch(movement, '随地传送解锁', '游戏会提示消耗穿行符，但实际不消耗。仅限已激活目的地，保留原有战斗／剧情等限制', self.teleport_unlocked, self.toggle_teleport)
        ttk.Label(movement, text='会提示消耗穿行符，但实际不消耗。', style='Muted.Card.TLabel', wraplength=400).pack(anchor='w', pady=(5, 0))
        self.teleport_status = tk.StringVar(value='传送：关闭')
        ttk.Label(movement, textvariable=self.teleport_status, style='Muted.Card.TLabel', wraplength=400).pack(anchor='w', pady=(5, 0))
        dust = self.group(right, '意识尘埃', '单次数量编辑，可撤销本窗口上次设置')
        button = ttk.Button(dust, text='意识尘埃数量', command=lambda: DustDialog(self.root, lambda: self.game))
        button.pack(fill='x', pady=(6, 2))
        self.controls.append(button)
        ttk.Label(dust, text='游戏显示可能在正常获得／消耗后刷新。', style='Muted.Card.TLabel').pack(anchor='w', pady=(8, 0))

        instructions = (
            ('开始使用', '先进入可操作角色的场景，再连接游戏。连接和读取不会修改数值；所有功能默认关闭。连接其他面板无法接管其已开启的功能。'),
            ('生命与资源', '强锁血维持生命下限，自动回血在受伤后补满，两者互斥。角色切换、死亡或读取异常时暂停写入，保留开启请求；当前玩家存活稳定并重新核对后自动恢复。资源锁约每 0.2 秒按需补满。葫芦／酒图标可能需要切换快捷药品刷新。'),
            ('倍率与移动', '攻击倍率与高攻击秒杀互相替代，不叠乘；游戏重算后维持本次目标，新角色稳定后重新应用。怪物倍率作用于玩家物理／元素承伤，可能覆盖同类环境伤害，不包含特殊真实伤害；锁血会掩盖效果。冲刺会被游戏重算，必要时重新应用移速。恢复移速不会关闭锁血。'),
            ('随地传送解锁', '游戏会提示消耗穿行符，但实际不消耗。已有穿行符记录且数量为 0 时同样可用。不添加背包物品、不解锁未激活目的地；保留原有战斗、剧情及其他可传送条件。关闭开关不影响锁血。正常退出撤销拦截，心跳超过约三秒未更新时拦截回到原有行为。'),
            ('关闭与恢复', '底部“恢复倍率、移速、冷却与锁血”也会恢复强锁血下限。正常关闭本窗口时，会停止本窗口的资源锁并尝试恢复倍率、移速、冷却及生命下限。意识尘埃可能随自动存档保留，不随退出回退。'),
            ('功能说明', '攻击倍率调整基础攻击，最终伤害由游戏计算。一键秒杀通过高攻击实现，特殊敌人和剧情规则仍按游戏处理。意识尘埃与资源数量可能需要正常获得、消耗或切换快捷药品后刷新显示。'))
        help_scroll = ttk.Scrollbar(help_page, orient='vertical')
        help_scroll.pack(side='right', fill='y')
        self.help_text = tk.Text(help_page, bg=BG, fg='#c2c8d3', font=('Microsoft YaHei UI', 10),
                                 wrap='word', relief='flat', borderwidth=0, highlightthickness=0,
                                 padx=4, pady=4, spacing1=4, spacing3=10,
                                 selectbackground='#5a343d', yscrollcommand=help_scroll.set, cursor='arrow')
        self.help_text.pack(fill='both', expand=True)
        help_scroll.configure(command=self.help_text.yview)
        self.help_text.tag_configure('heading', foreground='#eceef3', font=('Microsoft YaHei UI', 12, 'bold'))
        for heading, note in instructions:
            self.help_text.insert('end', heading + '\n', 'heading')
            self.help_text.insert('end', note + '\n\n')
        self.help_text.configure(state='disabled')
        ttk.Button(help_page, text='查看运行环境许可', command=self.show_licenses).pack(anchor='e', pady=(8, 0))
        footer = ttk.Frame(outer)
        footer.pack(fill='x', pady=(14, 0))
        self.effects = tk.StringVar(value='当前修改：无')
        ttk.Label(footer, textvariable=self.effects, style='Hint.TLabel', wraplength=620).pack(side='left')
        button = ttk.Button(footer, text='恢复倍率、移速、冷却与锁血', command=self.restore)
        button.pack(side='right')
        self.controls.append(button)
        self.message = tk.StringVar(value='就绪。连接游戏后，按需启用功能。')
        ttk.Label(outer, textvariable=self.message, style='Hint.TLabel', wraplength=960).pack(anchor='w', pady=(9, 0))
        # Step 4: graded error status bar. Red is sticky until "已知晓"; normal messages never overwrite it.
        self.issues = IssueQueue()
        self.issue_log = []
        self.rollback_warned = False
        status_bar = ttk.Frame(outer)
        status_bar.pack(fill='x', pady=(6, 0))
        self.issue_text = tk.StringVar(value='')
        self.issue_label = tk.Label(status_bar, textvariable=self.issue_text, bg=BG, fg=ISSUE_COLORS[''],
                                    font=('Microsoft YaHei UI', 9), anchor='w', justify='left', wraplength=620)
        self.issue_label.pack(side='left', fill='x', expand=True)
        ttk.Button(status_bar, text='已知晓', command=self.acknowledge_issues).pack(side='right')
        ttk.Button(status_bar, text='详情', command=self.show_issues).pack(side='right', padx=(0, 6))
        self.residual_button = ttk.Button(status_bar, text='恢复残留修改', command=self.restore)
        self.residual_button.pack(side='right', padx=(0, 6))
        self.controls.append(self.residual_button)
        self.enabled(False)
        if autoconnect:
            root.after(100, self.connect)
        root.after(1000, self.tick)
        root.after(200, self.resource_tick)
        root.after(50, self.health_tick)

    def fail(self, exc):
        """Show the error as before and grade it into the status bar (step 4)."""
        self.message.set(str(exc))
        category = classify(exc)
        if category == TARGET_GONE or getattr(exc, 'issue_reported', False):
            return
        if category == READ:
            self.issues.read_failed('', exc)
            return
        self.issues.report(category, '', exc)

    def drain_issues(self):
        new = self.issues.drain()
        if self.game is not None and getattr(self.game, 'issues', None) is not None:
            new += self.game.issues.drain()
        if not new:
            return
        self.issue_log = (self.issue_log + new)[-100:]
        color, text = summarize_issues(self.issue_log)
        lines = recent_lines(self.issue_log, 1)
        self.issue_text.set('⚠ ' + text + ('　最近：' + lines[-1] if lines else '') if text else '')
        self.issue_label.configure(fg=ISSUE_COLORS[color])
        if not self.rollback_warned and any(i['category'] == ROLLBACK for i in new):
            self.rollback_warned = True
            messagebox.showwarning('回滚失败，可能有残留修改',
                                   '\n'.join(recent_lines([i for i in new if i['category'] == ROLLBACK]))
                                   + '\n\n撤销记录已保留。请点底部“恢复残留修改”重试恢复；详情见 '
                                   + str(log_directory() / 'actions.jsonl'))

    def show_issues(self):
        lines = recent_lines(self.issue_log)
        messagebox.showinfo('最近 5 条错误', '\n'.join(lines) if lines else '当前没有错误记录。')

    def acknowledge_issues(self):
        self.issue_log = []
        self.rollback_warned = False
        self.issue_text.set('')
        self.issue_label.configure(fg=ISSUE_COLORS[''])

    def show_licenses(self):
        window = tk.Toplevel(self.root)
        window.title('运行环境许可')
        window.geometry('760x560')
        window.configure(bg=BG)
        scroll = ttk.Scrollbar(window, orient='vertical')
        scroll.pack(side='right', fill='y')
        text = tk.Text(window, bg=BG, fg='#c2c8d3', wrap='word', font=('Microsoft YaHei UI', 9),
                       relief='flat', padx=18, pady=18, yscrollcommand=scroll.set)
        text.pack(fill='both', expand=True)
        scroll.configure(command=text.yview)
        license_file = RESOURCE_HOME / 'assets' / 'THIRD_PARTY_NOTICES.txt'
        text.insert('end', license_file.read_text(encoding='utf8'))
        text.configure(state='disabled')

    def group(self, parent, title, note):
        card = ttk.Frame(parent, style='Card.TFrame', padding=(16, 10))
        card.pack(fill='x', pady=(0, 12))
        ttk.Label(card, text=title, style='Heading.Card.TLabel').pack(anchor='w')
        ttk.Label(card, text=note, style='Muted.Card.TLabel', wraplength=400).pack(anchor='w', pady=(2, 6))
        return card

    def switch(self, parent, label, note, variable, callback):
        row = ttk.Frame(parent, style='Card.TFrame')
        row.pack(fill='x', pady=3)
        control = ttk.Checkbutton(row, text=label, variable=variable, command=callback, style='Switch.TCheckbutton')
        control.pack(fill='x')
        control.tooltip = Tooltip(control, note)
        self.controls.append(control)
        return control

    def number_row(self, parent, title, note, variable, minimum, maximum, action_label, callback):
        row = ttk.Frame(parent, style='Card.TFrame')
        row.pack(fill='x', pady=7)
        label = ttk.Label(row, text=title, style='Card.TLabel', width=15)
        label.pack(side='left')
        control = ttk.Spinbox(row, from_=minimum, to=maximum, increment=0.1, textvariable=variable, width=8)
        control.pack(side='left', padx=(0, 8))
        control.tooltip = Tooltip(control, note)
        label.tooltip = Tooltip(label, note)
        button = ttk.Button(row, text='应用', command=callback)
        button.pack(side='right')
        button.action_label = action_label
        self.controls.append(button)
        return control

    def effect_session(self):
        session = getattr(self, '_effect_session', None)
        if session is None or session.game is not self.game:
            session = self._effect_session = SessionEffects(self.game)
        return session

    def scroll(self, event):
        if event.widget.winfo_toplevel() is self.root and event.widget.winfo_class() not in ('TSpinbox', 'TEntry', 'Entry', 'Spinbox', 'Text'):
            self.canvas.yview_scroll(-int(event.delta / 120), 'units')

    def enabled(self, enabled):
        for control in self.controls:
            control.state(['!disabled'] if enabled else ['disabled'])

    def connect(self):
        try:
            if self.game:
                self.game.stop_teleport_unlock()
                self.teleport_unlocked.set(False)
                self.teleport_status.set('传送：关闭')
                self.effect_session().clear_attack()
                self.game.stop_auto_health()
                self.auto_health.set(False)
                self.game.stop_auto_shachi()
                self.auto_shachi.set(False)
                self.game.stop_auto_spirit_shadow()
                self.auto_spirit_shadow.set(False)
                self.game.stop_auto_medicine()
                self.game.stop_auto_ultimate()
                for variable in (self.auto_gourd, self.auto_wine, self.auto_ultimate):
                    variable.set(False)
                result = self.game.restore_modifiers()
                self.floor_lock.set(False)
                self.health_status.set('生命保护：关闭')
                if self.game.undo:
                    raise RuntimeError('仍有未恢复记录，请先恢复或关闭：' + str(result))
                self.game.close()
                self.game = None
            self.game = Game()
            self.connection.set('●  已连接')
            self.enabled(True)
            self.refresh()
            self.message.set('已连接。补充按钮单次执行；倍率和冷却可手动恢复，资源锁定默认关闭。')
        except Exception as exc:
            self.connection.set('●  连接不可用')
            self.enabled(False)
            self.fail(exc)

    def refresh(self):
        if not self.game:
            return
        state = self.game.status()
        self.connection.set('●  已连接')
        med = '未找到' if state['medicine'] is None else str(state['medicine'])
        main, shachi = state['weapon_cooldowns']
        self.metrics['health'].set(f"{state['health']:.0f} / {state['health_max']:.0f}")
        self.metrics['shachi'].set(f"{state['shachi']:.0f} / {state['shachi_max']:.0f}")
        self.metrics['ultimate'].set(f"{state['ultimate_count']:.0f} / {state['ultimate_count_max']:.0f}")
        self.metrics['medicine'].set(med)
        self.values.set(f"生命  {state['health']:.1f} / {state['health_max']:.1f}     杀气  {state['shachi']:.1f} / {state['shachi_max']:.1f}\n"
                        f"基础攻击  {state['attack']:.1f}     葫芦次数  {med}     切武器  {main:.2f} / {shachi:.2f} 秒\n"
                        f"精纯杀气积累  {state['ultimate_charge']:.1f} / {state['ultimate_charge_max']:.1f}     点数  {state['ultimate_count']:.0f} / {state['ultimate_count_max']:.0f}")
        active = []
        if 'attack' in self.game.undo:
            high = self.game.undo['attack'].get('attack_mode') == 'one_hit'
            active.append('一键秒杀（高攻击）' if high else '基础攻击')
            self.one_hit.set(high)
        else:
            self.one_hit.set(False)
        if 'health_floor' in self.game.undo:
            active.append('生命下限')
        if any(key.startswith('incoming:') for key in self.game.undo):
            active.append('怪物伤害（玩家承伤）')
        if any(key.startswith('movement:') for key in self.game.undo):
            active.append('移动加速')
        if any(key == 'cooldown' or key.startswith('weapon_cooldown:') for key in self.game.undo):
            active.append('武器切换冷却')
        for label, variable in (('自动回血', self.auto_health), ('锁杀气', self.auto_shachi),
                                ('锁暗源', self.auto_spirit_shadow), ('锁葫芦', self.auto_gourd),
                                ('锁酒', self.auto_wine), ('锁精纯杀气', self.auto_ultimate),
                                ('随地传送解锁', self.teleport_unlocked)):
            if variable.get():
                active.append(label)
        self.effects.set('当前修改：' + ('、'.join(active) or '无'))

    def require_game(self):
        if not self.game:
            raise RuntimeError('请先连接游戏')

    def refill(self, feature):
        try:
            self.require_game()
            self.game.refill(feature)
            self.refresh()
            self.message.set({'health': '已单次回满生命，未锁血。', 'shachi': '已单次回满普通杀气。', 'medicine': '实际次数已补充。若游戏图标仍显示旧数字，切换一次快捷药品再切回即可刷新，无需重复点击。'}[feature])
        except Exception as exc:
            self.fail(exc)

    def modify(self, feature, text):
        try:
            self.require_game()
            self.game.modifier(feature, float(text))
            if feature == 'attack':
                self.effect_session().capture_attack()
            self.refresh()
            self.message.set({'cooldown': '已缩短角色切换间隔与当前武器自身冷却；换装备后需重新应用，可随时恢复。',
                              'movement': '已应用行走、跑步、冲刺和蹲行倍率；可用“恢复移速”单独撤销。',
                              'incoming': '已调整玩家物理／元素承伤系数，未改玩家攻击；不含特殊真实伤害。请关闭生命保护后对比同一招式，或保留设置稍后观察。'
                              }.get(feature, '已应用并读回确认。请观察实战效果；可随时恢复原值。'))
        except Exception as exc:
            self.fail(exc)

    def toggle_floor(self):
        try:
            self.require_game()
            if self.floor_lock.get():
                self.game.stop_auto_health()
                self.auto_health.set(False)
                session = self.effect_session()
                session.floor_disabled = False
                session.floor_step()
                self.health_status.set(session.floor_status)
                self.message.set('已单次补满并维持生命下限；游戏重算时自动重设下限，不循环回填血量。')
            else:
                result = self.game.stop_floor_lock()
                self.message.set(result or '生命下限原值已恢复。')
                self.health_status.set('生命保护：关闭')
            self.refresh()
        except Exception as exc:
            self.floor_lock.set(False)
            self.auto_health.set(False)
            self.health_status.set('生命保护已停止：' + str(exc))
            self.fail(exc)

    def toggle_one_hit(self):
        try:
            self.require_game()
            if self.one_hit.get():
                self.game.start_one_hit()
                self.message.set('已按原始基础攻击设为10000倍并持续维持，替代普通倍率；特殊敌人与剧情规则仍按游戏处理。取消勾选恢复原攻击。')
            else:
                result = self.game.stop_one_hit()
                self.message.set(result or '已关闭高攻击，恢复原始基础攻击；普通倍率可重新应用。')
            self.effect_session().capture_attack()
            self.refresh()
        except Exception as exc:
            record = self.game.undo.get('attack') if self.game else None
            self.one_hit.set(bool(record and record.get('attack_mode') == 'one_hit'))
            if self.game:
                self.effect_session().capture_attack()
            self.fail(exc)

    def toggle_health(self):
        try:
            self.require_game()
            if self.auto_health.get():
                self.game.start_auto_health()
                self.floor_lock.set(False)
                self.health_status.set('自动回血：开启（备用）')
                self.message.set('自动回血已开启，生命下限锁定已关闭；可取消勾选停止，无法保证拦截一击致死。')
            else:
                self.game.stop_auto_health()
                self.message.set('锁血已关闭，不再自动回血。')
                self.health_status.set('生命保护：关闭')
        except Exception as exc:
            self.auto_health.set(False)
            self.health_status.set('自动回血已停止：' + str(exc))
            self.fail(exc)

    def health_tick(self):
        if self.game and self.floor_lock.get():
            session = self.effect_session()
            session.floor_step()
            self.health_status.set(session.floor_status)
            if session.floor_disabled:
                self.floor_lock.set(False)
        try:
            if self.game and self.auto_health.get():
                self.game.pulse_auto_health()
        except Exception as exc:
            self.auto_health.set(False)
            self.health_status.set('自动回血已停止：' + str(exc))
            self.fail(exc)
        self.root.after(50, self.health_tick)

    def toggle_shachi(self):
        try:
            self.require_game()
            if self.auto_shachi.get():
                self.game.start_auto_shachi()
                self.message.set('锁杀气已开启，消耗后约每0.2秒检查并补满；关闭开关即停止。')
            else:
                self.game.stop_auto_shachi()
                self.message.set('锁杀气已关闭。')
        except Exception as exc:
            self.auto_shachi.set(False)
            self.fail(exc)

    def resource_tick(self):
        if self.game:
            self.effect_session().attack_step()
        if self.game and self.teleport_unlocked.get():
            self.teleport_status.set(self.game.pulse_teleport_unlock())
        try:
            if self.game and self.auto_shachi.get():
                self.game.pulse_auto_shachi()
        except Exception as exc:
            self.auto_shachi.set(False)
            self.fail(exc)
        for kind, variable in [('Hulu', self.auto_gourd), ('FeiJiu', self.auto_wine), ('ultimate', self.auto_ultimate),
                               ('spirit_shadow', self.auto_spirit_shadow)]:
            try:
                if self.game and variable.get():
                    if kind == 'ultimate':
                        self.game.pulse_auto_ultimate()
                    elif kind == 'spirit_shadow':
                        self.game.pulse_auto_spirit_shadow()
                    else:
                        self.game.pulse_auto_medicine(kind)
            except Exception as exc:
                variable.set(False)
                self.message.set({'ultimate': '精纯杀气', 'Hulu': '葫芦', 'FeiJiu': '酒', 'spirit_shadow': '暗源'}[kind] + '锁定已停止：' + str(exc))
        self.root.after(200, self.resource_tick)

    def toggle_teleport(self):
        try:
            self.require_game()
            if self.teleport_unlocked.get():
                self.teleport_status.set(self.game.start_teleport_unlock())
                self.message.set('随地传送解锁已请求启用。游戏会提示消耗穿行符，但实际不消耗。该开关不改变锁血。')
            else:
                self.game.stop_teleport_unlock()
                self.teleport_status.set('传送：关闭')
                self.message.set('随地传送解锁已关闭，锁血保持原状。')
            self.refresh()
        except Exception as exc:
            self.teleport_unlocked.set(False)
            self.teleport_status.set('传送：未启用／已停用，' + str(exc))
            self.fail(exc)

    def toggle_resource(self, kind, variable):
        try:
            self.require_game()
            if kind == 'ultimate':
                self.game.start_auto_ultimate() if variable.get() else self.game.stop_auto_ultimate()
            elif kind == 'spirit_shadow':
                self.game.start_auto_spirit_shadow() if variable.get() else self.game.stop_auto_spirit_shadow()
            elif variable.get():
                self.game.start_auto_medicine(kind)
            else:
                self.game.stop_auto_medicine(kind)
            self.message.set('已开启消耗后自动补满，约每0.2秒检查。药品图标若未刷新，请切换一次快捷药品再切回。'
                             if variable.get() else '该资源锁定已停止；已补充的数量保留。')
        except Exception as exc:
            variable.set(False)
            self.fail(exc)

    def ultimate(self):
        try:
            self.require_game()
            self.game.refill_ultimate()
            self.refresh()
            self.message.set('已补满精纯杀气积累与点数，用于释放主武器绝技。游戏界面可能需要刷新。')
        except Exception as exc:
            self.fail(exc)

    def restore_movement(self):
        try:
            self.require_game()
            results = self.game.restore_movement()
            self.refresh()
            self.message.set('；'.join(key.split(':', 1)[1] + '：' + value for key, value in results.items()) or '当前没有本面板的移动加速记录。')
        except Exception as exc:
            self.fail(exc)

    def restore(self):
        try:
            self.require_game()
            self.floor_lock.set(False)
            self.health_status.set('生命保护：已取消维持请求')
            self.effect_session().clear_attack()
            results = self.game.restore_modifiers()
            if 'health_floor' not in self.game.undo:
                self.floor_lock.set(False)
                if not self.auto_health.get():
                    self.health_status.set('生命保护：关闭')
            self.refresh()
            self.message.set('；'.join(k + '：' + v for k, v in results.items()) or '当前没有需要恢复的攻击、怪物倍率、移速、冷却或生命下限修改。')
        except Exception as exc:
            self.fail(exc)

    def tick(self):
        try:
            if self.game:
                session = self.effect_session()
                session.attack_step()
                if session.attack_status and ('暂缓' in session.attack_status or '已停用' in session.attack_status):
                    self.message.set(session.attack_status)
            self.refresh()
            self.issues.read_ok()
        except Exception as exc:
            self.connection.set('●  玩家不可读')
            for metric in self.metrics.values():
                metric.set('—')
            self.values.set('当前玩家不可读；刷新不会写入游戏。')
            self.fail(exc)
        self.drain_issues()
        self.root.after(1000, self.tick)

    def close(self):
        if self.game:
            try:
                self.game.stop_teleport_unlock()
            except Exception as exc:
                messagebox.showwarning('传送拦截恢复未完成', str(exc) + '\n已停止传送心跳；请保留面板并重试关闭。')
                return
            self.game.stop_auto_health()
            self.game.stop_auto_shachi()
            self.game.stop_auto_spirit_shadow()
            self.game.stop_auto_medicine()
            self.game.stop_auto_ultimate()
            result = self.game.restore_modifiers()
            if self.game.undo:
                messagebox.showwarning('部分数值未覆盖', '\n'.join(k + '：' + v for k, v in result.items()) + '\n记录保存在 ' + str(log_directory() / 'actions.jsonl'))
            self.game.close()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-check', action='store_true')
    parser.add_argument('--check-game', action='store_true', help='Read-only connection check; requires --self-check')
    parser.add_argument('--report')
    parser.add_argument('--preview', action='store_true', help='UI preview only; no game connection or writes')
    args = parser.parse_args()
    if args.check_game and not args.self_check:
        parser.error('--check-game requires --self-check')
    root = tk.Tk()
    if args.self_check:
        root.withdraw()
    panel = Panel(root, autoconnect=not (args.self_check or args.preview))
    if args.preview:
        root.title('影之刃零 · 0.13.1 界面预览')
        panel.connect_button.state(['disabled'])
        panel.connection.set('●  界面预览 · 未连接游戏')
        panel.message.set('预览模式：功能禁用，不连接或修改游戏。正在运行的修改器保持原状。')
    if args.self_check:
        class DustFixture:
            writes = 0

            def dust_state(self):
                return {'amount': 40461, 'token': (1, 2, 3, 4)}

            def set_dust(self, *args, **kwargs):
                self.writes += 1
                raise AssertionError('Self-check must never write')

        dust_fixture = DustFixture()
        dust_dialog = DustDialog(root, lambda: dust_fixture, visible=False)
        root.update()
        root.update_idletasks()
        def ui_texts(widget):
            values = []
            if 'text' in widget.keys():
                values.append(str(widget.cget('text')))
            if hasattr(widget, 'tooltip'):
                values.append(widget.tooltip.text)
            if isinstance(widget, tk.Text):
                values.append(widget.get('1.0', 'end'))
            for child in widget.winfo_children():
                values.extend(ui_texts(child))
            return values
        displayed_copy = '\n'.join([root.title(), *ui_texts(root)])
        profile = json.loads((RESOURCE_HOME / 'profile.json').read_text(encoding='utf8'))
        notices = (RESOURCE_HOME / 'assets' / 'THIRD_PARTY_NOTICES.txt').read_text(encoding='utf8')
        assert all(name in notices for name in ('Python', 'Tcl/Tk', 'PyInstaller'))
        game_check = None
        if args.check_game:
            class ReadOnlyGame(Game):
                def raw_write(self, *args, **kwargs):
                    raise AssertionError('Read-only check cannot write')
            check = ReadOnlyGame()
            try:
                state = check.status()
                game_check = {'pid': check.pid, 'version_verified': True, 'health': state['health'],
                              'health_max': state['health_max'], 'undo_records': len(check.undo), 'write_guard': True}
                assert not check.undo
            finally:
                check.close()
        result = {'created': True, 'frozen': bool(getattr(sys, 'frozen', False)),
                  'profile_version': profile['version'], 'runtime_licenses_embedded': True, 'steam_build': profile['steam_build'],
                  'persistent_log_directory': str(log_directory()), 'read_only_game_check': game_check, 'write_buttons': len(panel.controls), 'all_disabled': all(b.instate(['disabled']) for b in panel.controls),
                  'game_connected': panel.game is not None, 'auto_shachi_enabled': panel.auto_shachi.get(),
                  'auto_health_enabled': panel.auto_health.get(), 'visual_verified': False,
                  'floor_lock_enabled': panel.floor_lock.get(),
                  'one_hit_enabled': panel.one_hit.get(),
                  'teleport_enabled': panel.teleport_unlocked.get(),
                  'legacy_copy_present': any(word in displayed_copy.casefold() for word in ('\u5b9e\u9a8c', 'experimental')),
                  'teleport_consumption_notice': '会提示消耗穿行符，但实际不消耗' in displayed_copy,
                  'new_resource_locks': [panel.auto_gourd.get(), panel.auto_wine.get(), panel.auto_ultimate.get(), panel.auto_spirit_shadow.get()],
                  'dust_dialog_created': bool(dust_dialog.window.winfo_exists()),
                  'dust_range': [int(float(dust_dialog.amount_input.cget('from'))), int(float(dust_dialog.amount_input.cget('to')))],
                  'dust_initial_amount': dust_dialog.amount.get(), 'dust_fixture_writes': dust_fixture.writes,
                  'dust_undo_disabled': dust_dialog.undo_button.instate(['disabled']),
                  'attack_range': [float(panel.attack_input.cget('from')), float(panel.attack_input.cget('to'))],
                  'incoming_range': [float(panel.incoming_input.cget('from')), float(panel.incoming_input.cget('to'))],
                  'movement_range': [float(panel.movement_input.cget('from')), float(panel.movement_input.cget('to'))],
                  'scrollable': bool(panel.canvas.winfo_exists()),
                  'write_control_labels': [str(control.cget('text')) for control in panel.controls],
                  'content_required_height': panel.content.winfo_reqheight(),
                  'tabs': [panel.tabs.tab(tab, 'text') for tab in panel.tabs.tabs()],
                  'switch_count': sum(isinstance(c, ttk.Checkbutton) for c in panel.controls),
                  'help_readonly': str(panel.help_text.cget('state')) == 'disabled',
                  'preview_connect_disabled': panel.connect_button.instate(['disabled']) if args.preview else None}
        if args.report:
            Path(args.report).write_text(json.dumps(result, indent=2), encoding='utf8')
        if sys.stdout is not None:
            print(json.dumps(result))
        panel.close()
        assert result['tabs'] == ['功能面板', '使用说明'] and result['switch_count'] == 9 and result['help_readonly']
        if args.preview:
            assert result['preview_connect_disabled']
        assert result['all_disabled'] and not result['game_connected'] and not result['auto_shachi_enabled']
        assert not result['auto_health_enabled']
        assert not result['floor_lock_enabled']
        assert not result['legacy_copy_present'] and result['teleport_consumption_notice']
        assert not result['teleport_enabled'] and '随地传送解锁' in result['write_control_labels']
        assert not result['one_hit_enabled'] and result['attack_range'] == [0.1, 20.0]
        assert not any(result['new_resource_locks'])
        assert result['incoming_range'] == [0.1, 20.0]
        assert result['movement_range'] == [1.0, 5.0] and result['scrollable']
        assert '回满杀气' not in result['write_control_labels']
        assert '锁杀气' in result['write_control_labels']
        assert '补满精纯杀气' in result['write_control_labels']
        assert '锁精纯杀气' in result['write_control_labels']
        assert not any('终极' in label for label in result['write_control_labels'])
        assert not any('材料' in label for label in result['write_control_labels'])
        assert result['dust_dialog_created'] and result['dust_range'] == [0, 2000000000]
        assert result['dust_fixture_writes'] == 0 and result['dust_undo_disabled'] and result['dust_initial_amount'] == '40461'
    else:
        try:
            root.mainloop()
        finally:
            if panel.game and panel.game.handle:
                panel.game.stop_teleport_unlock()
                panel.game.stop_auto_health()
                panel.game.stop_auto_shachi()
                panel.game.stop_auto_spirit_shadow()
                panel.game.stop_auto_medicine()
                panel.game.stop_auto_ultimate()
                panel.game.restore_modifiers()
                panel.game.close()


if __name__ == '__main__':
    main()
