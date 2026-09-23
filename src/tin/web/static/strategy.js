(() => {
  const notice = document.getElementById('strategy-notice');
  const show = (message, error = false) => {
    if (!notice) return;
    notice.textContent = message;
    notice.className = `strategy-notice visible ${error ? 'error' : ''}`;
  };
  const request = async (url, body = {}) => {
    const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '操作失败');
    return data;
  };

  document.getElementById('generate-strategies')?.addEventListener('click', async event => {
    const button = event.currentTarget;
    button.disabled = true;
    show('正在核对当日具体合约行情、产业证据和有效研究判断…');
    try {
      const result = await request('/api/sn/strategies/generate', {date: new URLSearchParams(location.search).get('date')});
      if (result.status === 'generated') {
        show(`已生成 ${result.strategies.length} 条待审阅模拟草稿。`);
        location.reload();
      } else {
        show((result.gaps || ['暂无可用策略']).join('；'), true);
      }
    } catch (error) { show(error.message, true); }
    finally { button.disabled = false; }
  });

  document.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', async () => {
    const action = button.dataset.action;
    const id = window.STRATEGY_ID;
    if (!id) return;
    const url = `/api/sn/strategies/${id}/${action}`;
    let body = {};
    if (action === 'cancel') {
      const reason = prompt('请说明作废原因：');
      if (!reason?.trim()) return;
      body = {reason: reason.trim()};
    } else if (action === 'open' || action === 'close') {
      const prices = [...document.querySelectorAll('[data-fill-price]')];
      const costs = Number(document.getElementById('fill-costs')?.value || 0) / prices.length;
      const fills = prices.map(input => {
        const contract = input.dataset.fillPrice;
        const time = document.querySelector(`[data-fill-time="${contract}"]`).value;
        return {contract, price: Number(input.value),
          quantity: Number(document.querySelector(`[data-fill-qty="${contract}"]`).value),
          filled_at: `${time}:00+08:00`, costs};
      });
      if (fills.some(fill => !fill.price || !fill.quantity || !fill.filled_at)) {
        show('请完整填写每条腿的成交价格、手数和时间。', true); return;
      }
      const reason = document.getElementById('fill-reason')?.value?.trim();
      if (!reason) { show('请填写成交说明。', true); return; }
      body = {fills, reason};
    } else {
      const promptText = action === 'publish' ? '确认已审阅策略依据与风险，发布并进入等待入场状态？' : '确认操作？';
      if (!confirm(promptText)) return;
      body = {reason: '研究员审阅并确认'};
    }
    button.disabled = true;
    try { await request(url, body); location.reload(); }
    catch (error) { show(error.message, true); button.disabled = false; }
  }));
})();
