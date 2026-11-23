import {datasetField, feedback} from '../components/common/fields.js';
import {empty, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {handler} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, p} from '../utils/dom.js';
import {date} from '../utils/format.js';

// 从配对数据里汇总各扫描批次、各票种的可用票数，作为票量表的占位与提示。
function summarize(rows) {
  const batches = new Map();
  const types = new Map();
  for (const row of rows) {
    batches.set(row.batch_id, (batches.get(row.batch_id) || 0) + 1);
    types.set(row.document_type, (types.get(row.document_type) || 0) + 1);
  }
  return {batches, types};
}

function quotaInputs(active, dimension, map) {
  const names = [...map.keys()].sort();
  return el('fieldset', {class:'quota-grid'},
    el('legend', {}, dimension === 'batches' ? '各扫描批次票量' : '各票种票量'),
    names.map(name => {
      const current = active?.quotas?.[dimension]?.[name] ?? 0;
      return el('label', {class:'field'}, el('span', {}, `${name}（可用 ${map.get(name)}）`),
        el('input', {
          name:`${dimension}.${name}`, type:'number', min:'0', max:'100000', step:'1',
          value:current, required:true, inputMode:'numeric',
        }));
    }));
}

function collectQuotas(form) {
  const quotas = {batches: {}, types: {}};
  for (const [key, raw] of new FormData(form).entries()) {
    const dot = key.indexOf('.');
    if (dot <= 0) continue;
    const dimension = key.slice(0, dot);
    const name = key.slice(dot + 1);
    if (dimension !== 'batches' && dimension !== 'types') continue;
    const amount = Number(raw);
    if (!Number.isInteger(amount) || amount < 0) throw new Error(`「${name}」票量须为非负整数。`);
    quotas[dimension][name] = amount;
  }
  return quotas;
}

export async function quotaPage(route, refresh) {
  const {datasets} = workspace;
  const message = feedback();
  if (!datasets.length) {
    return panel('票量凑批', empty('请先在配对明细导入数据，再制定票量计划。', '#inputs', '前往导入'));
  }
  const selected = datasets.find(item => item.id === route.id) || datasets[0];
  const detail = await workspace.getDataset(selected.id, 0);
  const {batches, types} = summarize(detail.rows);
  const plans = await workspace.plans(selected.id).catch(() => []);
  const active = plans.find(item => !item.superseded) || null;

  const publish = button('发布票量计划（更正生成新版本）', {type:'submit', class:'primary', 'data-testid':'publish-plan'});
  const runButton = button('按当前计划发起凑批运行', {class:'primary', 'data-testid':'start-quota-run'});

  const form = el('form', {class:'fields', onSubmit: handler(publish, message, async event => {
    event.preventDefault();
    const quotas = collectQuotas(event.target);
    const saved = await workspace.publishPlan({dataset_id:selected.id, name:'票量凑批', quotas});
    message.textContent = `已发布版本 v${saved.version}` +
      (saved.invalidated_runs?.length ? `，并作废旧计划在途运行 ${saved.invalidated_runs.length} 条。` : '。');
    await refresh();
  })}, datasetField(datasets, selected.id),
    quotaInputs(active, 'batches', batches),
    quotaInputs(active, 'types', types),
    el('p', {class:'subtle'}, '两套票量总量须一致；计划更正可与后台计算并发，旧计划的在途运行会自动作废、结果不生效。'),
    el('div', {class:'actions'}, publish));

  // 切换数据批次时跳回对应计划页。
  form.querySelector('#dataset-id').addEventListener('change', event => {
    location.hash = `#quota/${event.target.value}`;
  });

  const runPanel = active ? panel('发起凑批运行',
    p(`当前生效版本 v${active.version}，运行将快照该版本与当前配对数据。`, 'subtle'),
    el('div', {class:'actions'}, runButton),
    el('p', {class:'feedback', 'aria-live':'polite', id:'quota-run-message'}))
    : panel('发起凑批运行', p('还没有生效计划，请先发布票量。', 'empty'));

  if (active) {
    runButton.addEventListener('click', handler(runButton, message, async () => {
      const item = await workspace.startQuotaRun(active.id, {dataset_id:selected.id, name:`票量凑批 v${active.version}`});
      location.hash = `#results/${item.id}`;
    }));
  }

  const history = table([
    {label:'版本', render:row => `v${row.version}`},
    {label:'状态', render:row => pill(row.superseded ? '已作废' : '生效中', row.superseded ? 'superseded' : 'complete')},
    {label:'批次票量', render:row => JSON.stringify(row.quotas.batches)},
    {label:'票种票量', render:row => JSON.stringify(row.quotas.types)},
    {label:'发布人', key:'author'},
    {label:'发布时间', render:row => date(row.created_at)},
  ], plans, {caption:'票量计划版本', empty:'首次发布后在此保留版本与作废记录。'});

  return el('div', {class:'two-col'},
    el('div', {class:'wide-panel'},
      panel('票量凑批', session.canWrite ? form : p('只读角色可查看当前票量与历史版本。', 'empty')),
      panel('计划版本', history)),
    el('aside', {class:'sidebar-panel'}, runPanel,
      panel('择优与冲突口径', el('ul', {class:'method-list'}, [
        '在同时凑齐各扫描批次、各票种两套票量的组合中，取 B 比 A 净增最低的一组票号。',
        '净增相同时，取排序后票号字典序最靠前的一组。',
        '凑不齐时给出冲突的批次与票种，以及要求票量与可用票量。',
        '验收页面展示与 CSV 下载读取同一份运行结果。',
      ].map(text => el('li', {}, text))))));
}
