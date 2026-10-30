const NS = 'http://www.w3.org/2000/svg';
const finite = value => typeof value === 'number' && Number.isFinite(value);

// Preserve nulls and outages: never interpolate across missing samples.
export function segments(points, metric, gapSeconds, start, end) {
  const result = []; let current = []; let previous = null;
  for (const point of points) {
    const t = Date.parse(point.timestamp);
    const value = point[metric];
    if (!Number.isFinite(t) || t < start || t > end || !finite(value)) {
      if (current.length) result.push(current);
      current = []; previous = null; continue;
    }
    if (previous !== null && (t <= previous || t - previous > gapSeconds * 1000)) {
      if (current.length) result.push(current);
      current = [];
    }
    current.push([t, value]); previous = t;
  }
  if (current.length) result.push(current);
  return result;
}
export function powerDomain(points) {
  const values = points.flatMap(p => [p.pv_power, p.home_power]).filter(finite).map(v => v / 1000);
  return [Math.min(0, Math.floor(Math.min(0, ...values))), Math.max(1, Math.ceil(Math.max(0, ...values)))];
}
function node(tag, attributes, text) {
  const element = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
  if (text !== undefined) element.textContent = text;
  return element;
}
function clock(time, zone) {
  return new Intl.DateTimeFormat(undefined, { timeZone: zone, hour: '2-digit', minute: '2-digit' }).format(new Date(time));
}
function chart(element, history, series, domain) {
  element.replaceChildren();
  const width = Math.max(320, element.clientWidth || 600);
  const height = Math.max(100, element.clientHeight || 150);
  element.setAttribute('viewBox', `0 0 ${width} ${height}`);
  const start = Date.parse(history.start), end = Date.parse(history.end);
  const x = t => 44 + (t - start) / (end - start) * (width - 54);
  const y = v => (height - 35) - (v - domain[0]) / (domain[1] - domain[0]) * (height - 50);
  for (let i = 0; i <= 2; i++) {
    const value = domain[0] + i * (domain[1] - domain[0]) / 2;
    element.append(node('line', { x1:44, x2:width - 10, y1:y(value), y2:y(value), class:'chart-grid' }));
    element.append(node('text', { x:35, y:y(value) + 4, 'text-anchor':'end', class:'chart-label' }, String(Math.round(value * 10) / 10)));
  }
  for (let i = 0; i <= 4; i++) {
    const t = start + i * (end - start) / 4;
    element.append(node('text', { x:x(t), y:height - 13, 'text-anchor':i === 0 ? 'start' : i === 4 ? 'end' : 'middle', class:'chart-label' }, clock(t, history.timezone)));
  }
  for (const [metric, color, scale] of series) {
    for (const part of segments(history.points, metric, history.gap_threshold_s, start, end)) {
      if (part.length === 1) element.append(node('circle', { cx:x(part[0][0]), cy:y(part[0][1] / scale), r:2.5, fill:color }));
      else element.append(node('path', {
        d:part.map(([t,v],i) => `${i ? 'L' : 'M'}${x(t).toFixed(2)},${y(v / scale).toFixed(2)}`).join(' '),
        fill:'none', stroke:color, 'stroke-width':2, 'stroke-linejoin':'round', 'vector-effect':'non-scaling-stroke',
      }));
    }
  }
}
export function renderHistory(history) {
  chart(document.getElementById('soc-chart'), history, [['soc','#9cddb0',1]], [0,100]);
  chart(document.getElementById('power-chart'), history, [['pv_power','#e7c37c',1000],['home_power','#91b9ed',1000]], powerDomain(history.points));

}
export function readingLabel(point, zone) {
  const number = (value, scale, unit) => finite(value) ? `${(value / scale).toFixed(scale === 1 ? 0 : 2)} ${unit}` : '—';
  return `${new Intl.DateTimeFormat(undefined, {timeZone:zone, month:'short', day:'numeric', hour:'2-digit', minute:'2-digit', second:'2-digit', timeZoneName:'short'}).format(new Date(point.timestamp))} · SOC ${number(point.soc,1,'%')} · Solar ${number(point.pv_power,1000,'kW')} · Home ${number(point.home_power,1000,'kW')}`;
}
