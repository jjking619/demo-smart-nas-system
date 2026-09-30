(function () {
  const I18N = {
    zh: {
      appTitle: '智能 NAS',
      leadText: '你的文件，一句话就够。',
      askTitle: '你想做什么？',
      catDocs: '文档',
      catDocsHint: '找文件 · 读合同 · 提要点',
      catPhotos: '照片',
      catPhotosHint: '搜图 · 分类 · 建相册',
      catVideos: '影音',
      catVideosHint: '下载 · 查找 · 播放',
      catAsk: '问 NAS',
      catAskHint: '随便问，或点下面的例子',
      entryLabel: '点击“说一句”按钮后说话，或呼叫“小远同学”唤醒；也可以打字直接输入。',
      voiceMode: '语音模式',
      voiceHint: '1. 点击开始  2. 立刻对麦克风说话  3. 等待助手播报',
      voiceBtnStart: '🎙 说一句',
      voiceBtnBusy: '🎙 正在录音...',
      textMode: '文本模式',
      textHint: '输入示例：播放测试视频 / 住房合同在哪',
      textInputPlaceholder: '也可以直接打字，一句话就行',
      textBtn: '发送',
      statusIdle: '待机中。点上面的卡片，或说话/打字。',
      turnsTitle: '最近活动',
      clearTurns: '清空',
      realTimeStatus: '实时状态',
      sourceWake: '🎤 唤醒',
      sourceButton: '🖱 按钮',
      sourceText: '⌨ 文本',
      stateAwake: '🎙 已唤醒，请说话...',
      stateListening: '🎙 正在聆听...',
      stateAsr: '🔍 正在识别...',
      stateProcessing: '⚙️ 正在处理...',
      stateSpeaking: '🔊 正在播报...',
      taskQueued: '请求已提交，正在排队...',
      taskCreated: '任务已创建：',
      taskStart: '开始执行...',
      taskQueryFailed: '任务查询失败：',
      triggerFailed: '触发失败：',
      requestFailed: '请求失败：',
      processingFailed: '处理失败：',
      taskDone: '处理完成',
      taskDoneWithCost: '处理完成，耗时',
      taskRetryBusy: '上一条仍在处理，正在自动重试...',
      taskBusyGiveup: '设备仍在忙，请稍后再试。',
      openResult: '已生成可跳转结果：点击打开',
      photoResultTitle: '找到的照片',
      photoSemanticSeaside: '海边',
      photoSemanticAnimals: '猫/动物',
      photoSemanticVintage: '复古滤镜预览',
      openInNasFiles: '在 NAS Files 打开',
      photoUnknownName: '未命名图片',
      pleaseInputText: '请输入文本指令。',
      turnCost: '耗时',
      unknownError: '未知错误',
    },
    en: {
      appTitle: 'Smart NAS',
      leadText: 'Your files, made easier.',
      askTitle: 'What would you like to do?',
      catDocs: 'Documents',
      catDocsHint: 'Find · summarize · extract key facts',
      catPhotos: 'Photos',
      catPhotosHint: 'Search · organize · create albums',
      catVideos: 'Videos',
      catVideosHint: 'Download · find · play',
      catAsk: 'Ask NAS',
      catAskHint: 'Ask anything, or try an example',
      entryLabel: 'Or just speak / type:',
      voiceMode: 'Voice mode',
      voiceHint: '1. Click begin  2. Speak into the mic immediately  3. Wait for the assistant response',
      voiceBtnStart: '🎙 Ask by voice',
      voiceBtnBusy: '🎙 Recording...',
      textMode: 'Text mode',
      textHint: 'Example: Play the sample video / Where is the housing contract?',
      textInputPlaceholder: 'Or just type - one sentence is enough',
      textBtn: 'Send',
      statusIdle: 'Idle. Pick a card above, or speak / type.',
      turnsTitle: 'Recent activity',
      clearTurns: 'Clear',
      realTimeStatus: 'Live status',
      sourceWake: '🎤 Wake',
      sourceButton: '🖱 Button',
      sourceText: '⌨ Text',
      stateAwake: '🎙 Awake, please speak...',
      stateListening: '🎙 Listening...',
      stateAsr: '🔍 Recognizing...',
      stateProcessing: '⚙️ Processing...',
      stateSpeaking: '🔊 Speaking...',
      taskQueued: 'request submitted and waiting in queue...',
      taskCreated: 'Task created: ',
      taskStart: 'starting execution...',
      taskQueryFailed: 'Task query failed: ',
      triggerFailed: 'Trigger failed: ',
      requestFailed: 'Request failed: ',
      processingFailed: 'Processing failed: ',
      taskDone: 'Completed',
      taskDoneWithCost: 'Completed in',
      taskRetryBusy: 'Previous task is still running. Retrying automatically...',
      taskBusyGiveup: 'Device is still busy. Please try again shortly.',
      openResult: 'A jump link is ready: open',
      photoResultTitle: 'Matched photos',
      photoSemanticSeaside: 'seaside',
      photoSemanticAnimals: 'cats / animals',
      photoSemanticVintage: 'vintage filter preview',
      openInNasFiles: 'Open in NAS Files',
      photoUnknownName: 'Unnamed image',
      pleaseInputText: 'Please enter a text command.',
      turnCost: 'Cost',
      unknownError: 'unknown error',
    }
  };

  function getPreferredLang() {
    try {
      const stored = window.localStorage.getItem('voice_ui_lang');
      if (stored === 'zh' || stored === 'en') return stored;
    } catch (_) {}

    const params = new URLSearchParams(window.location.search || '');
    const queryLang = params.get('lang');
    if (queryLang === 'zh' || queryLang === 'en') return queryLang;

    return 'zh';
  }

  function setStoredLang(lang) {
    try {
      window.localStorage.setItem('voice_ui_lang', lang);
    } catch (_) {}
  }

  function applyLang(lang) {
    const next = (lang === 'en') ? 'en' : 'zh';
    setStoredLang(next);
    document.documentElement.lang = next === 'en' ? 'en-US' : 'zh-CN';

    document.querySelectorAll('[data-i18n]').forEach((el) => {
      const key = el.dataset.i18n;
      if (!key) return;
      const val = I18N[next][key];
      if (val !== undefined) el.textContent = val;
    });

    document.querySelectorAll('[data-i18n-placeholder]').forEach((el) => {
      const key = el.dataset.i18nPlaceholder;
      const val = I18N[next][key];
      if (val !== undefined) el.setAttribute('placeholder', val);
    });

    const toggle = document.getElementById('langToggle');
    if (toggle) {
      toggle.textContent = next === 'en' ? '中文' : 'English';
      toggle.setAttribute('aria-label', next === 'en' ? 'Switch to Chinese' : 'Switch to English');
    }

    document.dispatchEvent(new CustomEvent('voice:lang-change', { detail: { lang: next } }));
  }

  window.voiceI18n = {
    getLang() {
      return getPreferredLang();
    },
    setLang(lang) {
      applyLang(lang);
    },
    applyLang,
    t(key, fallback = '') {
      const lang = getPreferredLang();
      const dict = I18N[lang] || I18N.zh;
      return dict[key] !== undefined ? dict[key] : (fallback || key);
    }
  };

  document.addEventListener('DOMContentLoaded', function () {
    applyLang(getPreferredLang());
    const toggle = document.getElementById('langToggle');
    if (toggle) {
      toggle.addEventListener('click', function () {
        const next = window.voiceI18n.getLang() === 'en' ? 'zh' : 'en';
        window.voiceI18n.setLang(next);
      });
    }
  });
})();
