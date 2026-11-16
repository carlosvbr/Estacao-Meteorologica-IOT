const TOKEN = 'SEU_TOKEN_AQUI';  // use o mesmo valor configurado no projeto
const ABA = 'Leituras';
 
function obterAba_() {
  const planilha = SpreadsheetApp.getActiveSpreadsheet();
  let aba = planilha.getSheetByName(ABA);
  if (!aba) aba = planilha.insertSheet(ABA);
 
  if (aba.getLastRow() === 0) {
    aba.appendRow(['Data/Hora', 'Temperatura (°C)', 'Umidade (%)', 'Índice de Calor (°C)', 'Gás MQ-2 (%)']);
    aba.getRange(1, 1, 1, 5).setFontWeight('bold').setBackground('#d9e2f3');
    aba.setFrozenRows(1);
  }
  return aba;
}
 
function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
 
function converterDataHora_(texto) {
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})$/.exec(texto || '');
  if (!m) return new Date();
  return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
}
 
function numeroOuVazio_(v) {
  if (v === undefined || v === null || v === '' || v === 'None') return '';
  const n = Number(String(v).replace(',', '.'));
  return isNaN(n) ? '' : n;
}
 
function gravarLeitura_(d) {
  const aba = obterAba_();
  aba.appendRow([
    converterDataHora_(d.dataHora),
    numeroOuVazio_(d.temperatura),
    numeroOuVazio_(d.umidade),
    numeroOuVazio_(d.indiceCalor),
    numeroOuVazio_(d.gasPct)
  ]);
  const linha = aba.getLastRow();
  aba.getRange(linha, 1).setNumberFormat('dd/MM/yyyy HH:mm:ss');
  return { ok: true, linha: linha };
}
 
function doGet(e) {
  try {
    const p = (e && e.parameter) || {};
    if (p.token !== TOKEN) return json_({ ok: false, erro: 'token invalido' });
    if (p.acao === 'gravar') return json_(gravarLeitura_(p));
    if (p.acao === 'resumo') return json_(resumoDoDia_(p.data));
    return json_({ ok: true, msg: 'Endpoint da estacao meteorologica ativo' });
  } catch (err) {
    return json_({ ok: false, erro: String(err) });
  }
}
 
function doPost(e) {
  try {
    const d = JSON.parse(e.postData.contents);
    if (d.token !== TOKEN) return json_({ ok: false, erro: 'token invalido' });
    return json_(gravarLeitura_(d));
  } catch (err) {
    return json_({ ok: false, erro: String(err) });
  }
}
 
function chaveData_(valor, fuso) {
  if (valor && typeof valor.getTime === 'function') {
    return Utilities.formatDate(valor, fuso, 'yyyy-MM-dd');
  }
  const texto = String(valor || '');
  let m = /^(\d{2})\/(\d{2})\/(\d{4})/.exec(texto);
  if (m) return m[3] + '-' + m[2] + '-' + m[1];
  m = /^(\d{4})-(\d{2})-(\d{2})/.exec(texto);
  if (m) return m[1] + '-' + m[2] + '-' + m[3];
  return null;
}
 
function resumoDoDia_(dataRef) {
  const fuso = Session.getScriptTimeZone();
  dataRef = dataRef || Utilities.formatDate(new Date(), fuso, 'yyyy-MM-dd');
 
  const aba = obterAba_();
  const total = aba.getLastRow() - 1;
  let qtd = 0, somaT = 0, somaU = 0;
  let tMin = null, tMax = null, uMin = null, uMax = null;
 
  if (total > 0) {
    SpreadsheetApp.flush();
    const valores = aba.getRange(2, 1, total, 3).getValues();
    valores.forEach(function (r) {
      if (chaveData_(r[0], fuso) !== dataRef) return;
      const t = Number(String(r[1]).replace(',', '.'));
      const u = Number(String(r[2]).replace(',', '.'));
      if (isNaN(t) || isNaN(u)) return;
      qtd++; somaT += t; somaU += u;
      tMin = tMin === null ? t : Math.min(tMin, t);
      tMax = tMax === null ? t : Math.max(tMax, t);
      uMin = uMin === null ? u : Math.min(uMin, u);
      uMax = uMax === null ? u : Math.max(uMax, u);
    });
  }
 
  const arred = function (v) { return Math.round(v * 100) / 100; };
  return {
    ok: true,
    data: dataRef,
    leituras: qtd,
    mediaTemp: qtd ? arred(somaT / qtd) : null,
    mediaUmid: qtd ? arred(somaU / qtd) : null,
    tempMin: tMin, tempMax: tMax,
    umidMin: uMin, umidMax: uMax
  };
}
 
function testar() {
  const fake = { postData: { contents: JSON.stringify({
    token: TOKEN, dataHora: '2026-11-13 10:00:00', temperatura: 25.5, umidade: 60, indiceCalor: 25.8, gasPct: 12 }) } };
  Logger.log(doPost(fake).getContent());
  Logger.log(doGet({ parameter: { token: TOKEN, acao: 'gravar', dataHora: '2026-11-13 10:00:30',
    temperatura: '26.5', umidade: '62', indiceCalor: '26.9', gasPct: '10' } }).getContent());
  Logger.log(JSON.stringify(resumoDoDia_('2026-11-13')));
}
 
