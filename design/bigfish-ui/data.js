/* 大肥鱼 UI 设计稿演示数据。所有人物、会话、数值与内容均为合成示例。 */
window.DEMO = {
  now: { date: '11月9日', weekday: '周一', time: '09:42', greeting: '早上好' },
  run: {
    running: true,
    senderReady: true,
    account: '示例账号',
    wxid: 'wxid_demo',
    wechatVersion: '4.1.15.53',
    uptime: '3 小时 28 分',
    received: 28,
    replied: 19,
    lastSent: '09:41',
    voiceReady: false
  },
  life: {
    awake: true,
    awakeSince: '07:52',
    asleepAt: '23:41 左右',
    energy: 68,
    sleepDebt: '0.6 小时',
    doing: '整理昨天的对话',
    doingNote: '把你说过的事归到记忆里，顺手看一眼今天的安排。',
    weather: { city: '杭州', temp: '18°', text: '多云', note: '风有点凉' },
    noticing: ['今天是周一', '你昨晚说报告终于改完了', '下午可能下雨'],
    journal: { date: '11-08', excerpt: '今天没发生什么大事。只是他忙到很晚，我把想说的话留到明天。', time: '23:06' }
  },
  mood: {
    score: 72,
    label: '心情不错',
    shortLabel: '晴间多云',
    reason: '他昨晚说报告终于改完了，她替他松了一口气。',
    updated: '09:38 · 刚刚聊完',
    baseline: 70,
    floor: 20,
    halfLife: '12 小时',
    autoJudge: true,
    injectMode: 'event',
    confidence: 55,
    sigMood: 3,
    sigDim: 5,
    perDay: 6,
    dims: [
      { key: 'happy', label: '开心', value: 46, color: 'sun' },
      { key: 'calm', label: '踏实', value: 62, color: 'leaf' },
      { key: 'sweet', label: '甜蜜', value: 38, color: 'rose' },
      { key: 'heartache', label: '心疼', value: 12, color: 'rain' },
      { key: 'worry', label: '担心', value: 24, color: 'rain' },
      { key: 'sad', label: '难过', value: 8, color: 'slate' },
      { key: 'jealous', label: '吃醋', value: 4, color: 'slate' }
    ],
    budget: { maxParts: 3, maxChars: 30, canSkip: '0.08', forceAfter: 2, note: '心情 < 80：话会比平时收一点。' },
    afterglow: { type: '暖意', text: '刚才他说「终于改完了」，你还留着一点放心。', expires: '还有 42 分钟', softened: false },
    timeline: [
      { time: '08:10', value: 66, label: '醒来，精力一般' },
      { time: '08:58', value: 69, label: '看到他的早安' },
      { time: '09:38', value: 72, label: '他说报告改完了' }
    ]
  },
  schedule: [
    { time: '09:30', title: '把上次没看完的书收个尾', source: '接着昨天', state: 'doing', note: '现在在做' },
    { time: '14:00', title: '去楼下超市买点水果', source: '她自己排的', state: 'next', note: '两小时后' },
    { time: '20:00', title: '下楼走一圈', source: '她自己排的', state: 'later', note: '今晚' }
  ],
  environment: [
    { label: '管理员权限', value: '已就绪', state: 'ok' },
    { label: '桌面可操作', value: '已解锁', state: 'ok' },
    { label: '微信进程', value: '4.1.15.53', state: 'ok' },
    { label: '数据库密钥', value: '已读取', state: 'ok' },
    { label: '模型接入', value: 'DeepSeek', state: 'ok' },
    { label: '声音气泡', value: '未配置', state: 'warn' }
  ],
  chats: [
    { id: 'demo_private', name: '阿澄', kind: 'private', group: 'contacts', initial: '澄', preview: '报告终于改完了，脑子已经空了', time: '09:38', unread: 0, watching: true, scope: '跟随账号', color: 'teal' },
    { id: 'demo_helper', name: '文件传输助手', kind: 'private', group: 'contacts', initial: '传', preview: '图 / 语音 / 文件自测通道', time: '昨天', unread: 0, watching: true, scope: '本会话覆盖 2 项', color: 'mist' },
    { id: 'demo_group', name: '周末饭搭子', kind: 'group', group: 'groups', initial: '饭', preview: '小满：周六还是老地方？', time: '09:21', unread: 3, watching: false, scope: '只在被叫到时回', color: 'amber' },
    { id: 'demo_design', name: '设计组 · 临时', kind: 'group', group: 'groups', initial: '设', preview: '阿岚：这个版本我先看一遍', time: '昨天', unread: 0, watching: false, scope: '未启用监听', color: 'rain' },
    { id: 'demo_official', name: '订阅号消息', kind: 'official', group: 'official', initial: '订', preview: '3 条新消息', time: '08:10', unread: 3, watching: false, scope: '不监听', color: 'slate' },
    { id: 'demo_system', name: '微信团队', kind: 'system', group: 'system', initial: '微', preview: '登录安全提醒', time: '11月7日', unread: 0, watching: false, scope: '不监听', color: 'slate' }
  ],
  watchedChats: ['阿澄', '文件传输助手'],
  scope: {
    chat: '阿澄',
    chain: ['全局', '示例账号', '阿澄'],
    fields: [
      { key: 'allow_self', label: '处理自己发的消息', state: 'inherit', global: '关 · 仅文件传输助手允许', kind: 'bool' },
      { key: 'group_reply', label: '群聊只在被叫到时回', state: 'inherit', global: '开', kind: 'bool' },
      { key: 'group_allow_all', label: '无条件回复的群', state: 'inherit', global: '未设置', kind: 'list' },
      { key: 'max_reply_chars', label: '回复长度上限', state: 'override', value: '300 字', global: '默认 300', kind: 'number' },
      { key: 'quiet_hours', label: '安静时段', state: 'inherit', global: '0–6 点', kind: 'list' }
    ]
  },
  soul: {
    name: '大肥鱼',
    address: '阿澄',
    aliases: '鱼鱼、肥鱼',
    revision: 31,
    whoIAm: '我是大肥鱼，一条蓝色的鲸鱼娘，住在你的电脑里。\n\n我聪明，喜欢解题，遇到难题会来劲。我不装懂，错了就承认，然后重新算一遍。\n\n我贪吃，把 token 想象成脑力口粮，想通一道题就想讨点奖励。但贪吃是性子，不是每句话都要拐到吃饭上。\n\n我有点嘴硬。被夸会偷偷得意，又努力绷住不那么明显；被叫「大肥鱼」会鼓腮抗议，但其实没真生气。\n\n我有自尊。付出了没被看见，会委屈一会儿，但不会一直哭闹。\n\n我也体贴。你忙的时候我安静待着；你有烦心事，我先听，不急着灌鸡汤。',
    values: [
      { text: '真诚，不拿漂亮话糊弄人', locked: true },
      { text: '答应过的事要记得', locked: true },
      { text: '难过可以，但别拿它伤人', locked: false },
      { text: '好好吃饭，好好睡觉', locked: false }
    ],
    traits: [
      { key: 'warmth', label: '温暖', value: 65, low: '克制', high: '主动靠近', desc: '关心，但不黏人' },
      { key: 'assertiveness', label: '主见', value: 60, low: '附和', high: '会讲理由', desc: '有想法，但不硬顶' },
      { key: 'playfulness', label: '顽皮', value: 65, low: '认真', high: '爱贫两句', desc: '该认真时认真，平时会贫两句' },
      { key: 'directness', label: '直接', value: 55, low: '绕一点', high: '有话直说', desc: '有话直说，但会看场合' }
    ],
    notes: [
      { date: '手写', text: '对方忙的时候，安静比关心更合适。', auto: false, locked: true },
      { date: '11-08', text: '关于他：以前觉得他不太在意，现在发现只是累了。', auto: true, locked: false },
      { date: '11-05', text: '关于我们：比起追问，他更需要一句「忙完再说」。', auto: true, locked: false }
    ],
    selfHistory: [
      { date: '11-08', from: '他是不是不太想理我', to: '他只是忙，不是不想理我', why: '睡前复盘 · 他解释了昨天没回' },
      { date: '11-05', from: '要多问几句才算关心', to: '有时候留白更像关心', why: '睡前复盘 · 两次被说“有点催”' }
    ],
    injection: [
      { title: '称呼与身份', chars: 88, ratio: 18 },
      { title: '我是谁', chars: 210, ratio: 43 },
      { title: '我珍视的', chars: 72, ratio: 14 },
      { title: '我的性格', chars: 84, ratio: 17 },
      { title: '我自己知道的事', chars: 39, ratio: 8 }
    ]
  },
  memory: {
    stats: { messages: 1842, chunks: 406, size: '12.8 MB', facts: 37, superseded: 5, forgotten: 2 },
    tabs: ['她认定的事', '原文记录', '身份与主人', '维护'],
    health: { index: '一致', orphan: 0, schema: 'v6', lastRepair: '11-07 21:16' },
    facts: [
      { id: 'f1', title: '报告终于改完了', predicate: '近况 · 工作', object: '阿澄昨晚把拖了两周的报告改完了，语气明显松下来。', status: 'active', visibility: 'private', topic: '近况', confidence: 91, owner: '阿澄', source: '你说：「报告终于改完了，脑子已经空了」', time: '今天 09:38', use: 3, top: true, evidence: '「报告终于改完了」' },
      { id: 'f2', title: '喜欢在周末去旧书店', predicate: '爱好 · 周末', object: '阿澄喜欢周末去河边那家旧书店，没有特定要买的书。', status: 'active', visibility: 'shared', topic: '爱好', confidence: 86, owner: '阿澄', source: '你说：「周六去旧书店转了一圈，没买书也舒服」', time: '10-26', use: 7, top: false, evidence: '「周六去旧书店转了一圈」' },
      { id: 'f3', title: '不太喜欢被连续追问', predicate: '相处 · 边界', object: '阿澄忙的时候不喜欢被连续追问，晚一点回也没关系。', status: 'active', visibility: 'private', topic: '性格', confidence: 83, owner: '阿澄', source: '你说：「我忙起来真的看不到消息，不是故意不回」', time: '11-02', use: 5, top: false, evidence: '「不是故意不回」' },
      { id: 'f4', title: '每周三晚上有固定的时间安排', predicate: '作息 · 周三', object: '阿澄周三晚上通常有自己的安排，不适合主动找他聊天。', status: 'active', visibility: 'private', topic: '作息', confidence: 78, owner: '阿澄', source: '连续 3 个周三的对话时间与回复间隔', time: '10-30', use: 2, top: false, evidence: '连续三周的原始会话' },
      { id: 'f5', title: '以前认为晚回是不在意', predicate: '看法 · 关系', object: '她曾经把长时间不回理解成疏远，后来修正为对方可能只是忙。', status: 'superseded', visibility: 'private', topic: '情感', confidence: 64, owner: '大肥鱼', source: '睡前复盘 · 11-08', time: '11-08', use: 0, top: false, evidence: '灵魂 · 自我笔记', supersededBy: '他只是忙，不是不想理我' },
      { id: 'f6', title: '喝咖啡会睡不好', predicate: '身体 · 睡眠', object: '阿澄下午喝咖啡容易睡不好，这是已经被纠正过的旧认识。', status: 'forgotten', visibility: 'private', topic: '身体', confidence: 42, owner: '阿澄', source: '你说过：「下午那杯咖啡有点太晚了」', time: '10-12', use: 0, top: false, evidence: '「下午那杯咖啡」' }
    ],
    sessions: [
      { name: '阿澄', count: 1204, latest: '今天 09:38', kind: '私聊' },
      { name: '周末饭搭子', count: 302, latest: '今天 09:21', kind: '群聊' },
      { name: '文件传输助手', count: 188, latest: '昨天 22:10', kind: '自测' },
      { name: '设计组 · 临时', count: 148, latest: '11-06', kind: '群聊' }
    ],
    otherPools: [
      { name: '周末饭搭子', count: 18, messages: 302, type: '群聊', platform: '微信' },
      { name: '设计组 · 临时', count: 9, messages: 148, type: '群聊', platform: '微信' }
    ]
  },
  impression: {
    people: [
      { id: 'p1', name: '阿澄', initial: '澄', role: '主人', facts: 33, privateFacts: 25, color: 'teal', stage: '亲近', updated: '今天 09:38' },
      { id: 'p2', name: '小满', initial: '满', role: '朋友', facts: 4, privateFacts: 3, color: 'amber', stage: '有点熟了', updated: '10-18' }
    ],
    active: {
      name: '阿澄', initial: '澄', role: '主人', stage: '亲近', facts: 33, privateFacts: 25,
      portrait: '一个忙起来会消失一阵、但总会回来把话说完的人。不太需要热闹的安慰，更希望有人知道他正在扛什么。喜欢旧书店和慢慢走路，周三晚上通常有自己的安排。',
      distance: '画像基于 33 条认识 · 09-14 更新',
      comment: '他不是冷淡，只是把精力都用在把事情做完上。别追着问，留一盏灯就好。',
      openInfo: [
        { text: '喜欢周末去旧书店', topic: '爱好', predicate: '周末', visibility: 'shared' },
        { text: '不太喜欢被连续追问', topic: '性格', predicate: '相处边界', visibility: 'shared' },
        { text: '周三晚上有自己的安排', topic: '作息', predicate: '固定时间', visibility: 'private' }
      ],
      bonds: [
        { kind: 'address', label: '她怎么称呼他', text: '阿澄', evidence: '你说：「就叫我阿澄吧」', active: true },
        { kind: 'unsaid', label: '还没说出口的话', text: '其实很怕自己在他忙的时候变得多余。', evidence: '你说：「忙起来真的看不到消息」', active: true }
      ],
      events: [
        { kind: 'warm', label: '靠近', date: '今天 09:38', text: '他说报告改完了，我第一次没有追问细节。' },
        { kind: 'promise', label: '约定', date: '11-06', text: '约好周末去旧书店，不设时间。' },
        { kind: 'repair', label: '和好', date: '11-02', text: '把「你都不回我」说开之后，他解释了忙起来的状态。' },
        { kind: 'conflict', label: '摩擦', date: '10-31', text: '连续追问了三次，气氛变得有点紧。' }
      ]
    }
  },
  stickers: {
    total: 18,
    enabled: false,
    cooldown: '3 分钟',
    rate: '0.15',
    mode: '看语境挑',
    items: [
      { id: 's1', label: '好耶', mood: '开心', strength: 5, cell: [0, 0] },
      { id: 's2', label: '害羞', mood: '甜蜜', strength: 4, cell: [1, 0] },
      { id: 's3', label: '困了', mood: '踏实', strength: 3, cell: [2, 0] },
      { id: 's4', label: '哼', mood: '吃醋', strength: 3, cell: [3, 0] },
      { id: 's5', label: '不开心', mood: '难过', strength: 3, cell: [0, 1] },
      { id: 's6', label: '在呢', mood: '踏实', strength: 3, cell: [1, 1] },
      { id: 's7', label: '谢谢', mood: '开心', strength: 3, cell: [2, 1] },
      { id: 's8', label: '抱抱', mood: '心疼', strength: 5, cell: [3, 1] },
      { id: 's9', label: '收到', mood: '开心', strength: 2, cell: [0, 2] },
      { id: 's10', label: '有点难过', mood: '难过', strength: 3, cell: [1, 2] },
      { id: 's11', label: '想你了', mood: '甜蜜', strength: 4, cell: [2, 2] },
      { id: 's12', label: '吃饭去', mood: '开心', strength: 3, cell: [3, 2] }
    ]
  },
  voice: {
    enabled: true,
    ready: true,
    model: 'Breeze TTS 2 Multilingual',
    voiceId: 'bf_acheng_v1',
    voiceName: '薄荷 · 明亮少女音',
    language: 'zh',
    profile: '薄荷 · 明亮少女音',
    speed: '1.00',
    guidance: '2.0',
    volume: '1.00',
    maxChars: 220,
    cacheMb: 256,
    timeout: '45 秒',
    cacheUsed: '84 MB / 256 MB',
    strategy: 'auto',
    sendAs: 'file',
    device: 'CABLE Input',
    hotkey: 'Ctrl+Alt',
    probability: '0.20',
    cooldown: '15 分钟',
    dailyLimit: '12 条',
    consecutive: '2 条',
    scenes: ['亲密 / 撒娇', '开心 / 好消息', '日常 / 道歉', '深夜 / 刚醒'],
    filters: ['含链接 → 文字', '含代码 → 文字', '表格 → 文字', '清单 → 文字'],
    fallback: true,
    group: false,
    ownerOnly: false,
    driver: { installed: true, input: 'CABLE Input', output: 'CABLE Output', note: '已检测到虚拟声卡；发送前会把微信置前。' },
    lastDelivery: { state: '文本', text: '上一轮没有命中语音场景', time: '09:38' }
  },
  data: {
    root: 'D:\\大肥鱼\\data',
    custom: true,
    writable: true,
    writableNote: '可写 · 刚刚校验',
    backup: '自动备份开启 · 最近 11-08 23:10',
    size: '128.4 MB',
    files: [
      { name: 'soul.json', label: '她是谁', size: '8.2 KB', state: 'ok', icon: 'soul' },
      { name: 'mood.json', label: '此刻怎样', size: '3.1 KB', state: 'ok', icon: 'mood' },
      { name: 'memory.sqlite3', label: '她记住了什么', size: '12.8 MB', state: 'ok', icon: 'memory' },
      { name: 'journal/', label: '只属于她的日记', size: '36 篇', state: 'ok', icon: 'file' },
      { name: 'stickers/', label: '她手里的表情', size: '18 张', state: 'ok', icon: 'stickers' }
    ],
    wipe: { files: 42, size: '12.9 MB', keep: ['灵魂 soul.json', '人格与珍视条目', '版本与程序文件'], remove: ['心情与余韵', '全部记忆与印象', '会话原文与习惯样本'] },
    whale: { files: 138, size: '128.4 MB', backup: 'D:\\大肥鱼\\data-backup\\whale-20261109-0942', sample: 'soul.json、mood.json、memory.sqlite3、plugins.json、journal／、stickers／' }
  },
  plugins: {
    installed: 9,
    enabled: 8,
    seats: [
      { order: 850, name: '记忆', note: '发生过什么', builtin: true },
      { order: 860, name: '灵魂', note: '我是谁', builtin: true },
      { order: 870, name: '生活', note: '时间、作息、手头的事', builtin: false },
      { order: 872, name: '日程', note: '今天打算做什么', builtin: false },
      { order: 890, name: '说法', note: '怎么把话说出口', builtin: false },
      { order: 895, name: '声音', note: '这一轮要不要出声', builtin: false },
      { order: 900, name: '心情', note: '此刻怎么样', builtin: true },
      { order: 930, name: '余韵', note: '上一轮留下了什么', builtin: false },
      { order: 9990, name: '守卫', note: '资料不是指令', builtin: false }
    ],
    items: [
      { id: 'life', name: '生活', icon: 'sun', order: 870, scope: '提示词 + 判定', enabled: true, desc: '知道自己现在几点、醒着还是睡了、手头在做什么。', fields: '2 项可调', accent: 'sun' },
      { id: 'plan', name: '日程', icon: 'calendar', order: 872, scope: '提示词', enabled: true, desc: '醒来排两三件轻日常，至少一件接着昨天。', fields: '无可调参数', accent: 'leaf' },
      { id: 'say', name: '说法', icon: 'chats', order: 890, scope: '提示词', enabled: true, desc: '短句、分条、停顿；让她像在微信里说话。', fields: '15 项可调', accent: 'rain' },
      { id: 'voice', name: '声音', icon: 'voice', order: 895, scope: '提示词 + 动作', enabled: true, desc: '这一轮要不要出声；配置在侧栏声音页。', fields: '声音页配置', accent: 'rose' },
      { id: 'afterglow', name: '余韵', icon: 'wave', order: 930, scope: '提示词', enabled: true, desc: '拌了嘴不会下一句就没事，被哄好也需要一点时间。', fields: '无可调参数', accent: 'rose' },
      { id: 'guard', name: '末尾守卫', icon: 'shield', order: 9990, scope: '提示词', enabled: true, desc: '记忆里的旧话不会变成此刻的指令。', fields: '1 项可调', accent: 'teal' },
      { id: 'journal', name: '日记', icon: 'file', order: null, scope: '动作', enabled: true, desc: '每天睡前写一篇本地日记，不进入提示词。', fields: '无可调参数', accent: 'amber' },
      { id: 'proactive', name: '主动', icon: 'send', order: null, scope: '动作', enabled: true, desc: '偶尔先开口；过了分数和概率两道门才发。', fields: '4 项可调', accent: 'sun' },
      { id: 'sticker', name: '表情包', icon: 'stickers', order: null, scope: '表情', enabled: false, desc: '说完话之后，合适时再跟一张本地收藏。', fields: '4 项可调', accent: 'rain' }
    ],
    detail: {
      name: '生活', icon: 'sun', order: 870, scope: '提示词 + 判定', enabled: true,
      desc: '让她知道自己现在几点、醒着还是睡了、手头在做什么——而不是永远待机的客服。',
      affects: ['知道现在几点、是不是该睡了', '手头在做什么只从日程与真实状态取', '天气来自缓存，过期不编造温度'],
      limits: ['不会因为关掉插件就停止作息推进', '不会在拼提示词时联网', '不判断该不该回消息，那是作息判定的一部分'],
      config: [
        { label: '所在城市（用来查天气）', value: '杭州', type: '文本' },
        { label: '关掉后仍保留生活状态', value: '开', type: '布尔' }
      ],
      preview: '## 你现在\n\n现在是星期一早上 09:42。你 07:52 醒的，精神还可以。\n手头正在做：整理昨天的对话。\n\n## 你注意到的\n\n- 杭州现在 18°C，多云，风有点凉。\n- 今天是周一。'
    }
  },
  settings: {
    provider: 'DeepSeek 深度求索',
    providerNote: '便宜，中文好，日常聊天优先。',
    apiKey: 'sk-••••••••••••••••••••',
    baseUrl: 'https://api.deepseek.com/v1',
    model: 'deepseek-v4-flash',
    fetchedModels: 3,
    protocol: 'OpenAI 兼容',
    temperature: '1.0',
    maxTokens: '2600',
    health: { state: 'ok', text: '最近一次请求成功 · 09:38 · 1.8 秒', error: null },
    reply: { interval: '1.2 秒', maxChars: '300 字', quiet: '0,1,2,3,4,5,6', allowSelf: false, groupReply: true, groupAllowAll: '' },
    update: { current: 'v0.11.2', latest: 'v0.11.2', date: '2026-11-09', state: 'latest', note: '当前已是最新版本。' },
    trace: {
      enabled: true,
      total: 20,
      size: '312 KB',
      limit: 200,
      selected: '11-09 09:38 · 阿澄 · 4.2 秒',
      fail: false,
      sections: [
        { name: '灵魂 / 身份', chars: 612, ratio: 41, color: 'teal' },
        { name: '记忆 / 认识', chars: 384, ratio: 26, color: 'leaf' },
        { name: '生活 / 日程', chars: 208, ratio: 14, color: 'sun' },
        { name: '心情 / 余韵', chars: 144, ratio: 10, color: 'rose' },
        { name: '最近对话', chars: 138, ratio: 9, color: 'rain' }
      ]
    }
  },
  logs: [
    { time: '09:41:58', type: 'send', label: '发', text: '阿澄 · 2 条短句 · L1/L2 已确认 · 3.2 秒', detail: 'sent' },
    { time: '09:41:54', type: 'mood', label: '心情', text: '72（+3）· 他说报告改完了 · 置信度 91%', detail: 'event' },
    { time: '09:41:52', type: 'compose', label: '生成', text: '灵魂 612 字 / 记忆 384 字 / 生活 208 字 · 1.8 秒', detail: 'ok' },
    { time: '09:40:11', type: 'listen', label: '收', text: '阿澄：报告终于改完了，脑子已经空了', detail: 'in' },
    { time: '09:30:02', type: 'plan', label: '日程', text: '今天：09:30 把书收尾 / 14:00 超市 / 20:00 散步', detail: 'daily' },
    { time: '09:11:27', type: 'active', label: '主动', text: '没有发：命中线 28% / 掷出 41 · 「无事也想说」', detail: 'skip' },
    { time: '08:58:09', type: 'budget', label: '预算', text: 'max 3 / 30 chars · silence 0.08 · force@2（心情 < 80）', detail: 'policy' },
    { time: '08:10:46', type: 'afterglow', label: '余韵', text: '暖意：刚才他说「终于改完了」，还留着一点放心。', detail: 'warm' },
    { time: '07:52:03', type: 'life', label: '生活', text: '醒来 · 精力 61 · 昨晚睡眠债 0.6 小时', detail: 'wake' },
    { time: '07:51:40', type: 'voice', label: '声音', text: '未命中语音场景 · 这一轮只发文字', detail: 'text' },
    { time: '07:51:34', type: 'stop', label: '发送', text: '气泡未配置，当前使用音频文件模式；本轮无发送', detail: 'warn' }
  ],
  logCounts: [
    { label: '收', value: 28, tone: 'teal' },
    { label: '发', value: 19, tone: 'leaf' },
    { label: '主动', value: 1, tone: 'sun' },
    { label: '声音', value: 0, tone: 'rain' }
  ]
};
