// Fetch and parse M3U, show selection UI, and POST selection to backend
async function fetchAndShowChannelSelect(m3uUrl) {
  const resp = await fetch(m3uUrl);
  const text = await resp.text();
  const lines = text.split(/\r?\n/);
  let channels = [];
  let currentGroup = '';
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (line.startsWith('#EXTINF')) {
      let name = line.split(',')[1] || 'unknown';
      let groupMatch = line.match(/group-title="([^"]+)"/);
      if (groupMatch) currentGroup = groupMatch[1];
      // Find next non-empty, non-comment line for URL
      let j = i + 1;
      while (j < lines.length && (!lines[j] || lines[j].startsWith('#'))) j++;
      let url = lines[j] || '';
      channels.push({name, url, group: currentGroup});
      i = j;
    }
  }
  // Build modal
  const modal = document.createElement('div');
  modal.id = 'channel-select-modal';
  modal.style = 'position:fixed;top:0;left:0;width:100vw;height:100vh;background:rgba(0,0,0,0.5);z-index:1000;display:flex;align-items:center;justify-content:center;';
  modal.innerHTML = `<div style="background:#fff;padding:24px 32px;border-radius:10px;max-width:480px;width:90vw;max-height:80vh;overflow:auto;box-shadow:0 2px 16px #0003;">
    <h2 style="margin-top:0;">Select Groups/Channels to Test</h2>
    <form id="channel-select-form">
      <div id="group-checkboxes" style="margin-bottom:12px;"></div>
      <div id="channel-checkboxes" style="max-height:30vh;overflow:auto;margin-bottom:16px;"></div>
      <button type="submit" style="padding:8px 20px;background:#4f8cff;color:#fff;border:none;border-radius:6px;font-size:1em;cursor:pointer;">Start Test</button>
    </form>
  </div>`;
  document.body.appendChild(modal);
  // Group list
  const groups = Array.from(new Set(channels.map(c => c.group)));
  const groupDiv = modal.querySelector('#group-checkboxes');
  groupDiv.innerHTML = groups.map((g,i) => `<label style='display:inline-block;margin-right:12px;'><input type='checkbox' class='groupbox' value='${g.replace(/'/g, "&#39;")}' checked> ${g}</label>`).join('');
  // Channel list
  const channelDiv = modal.querySelector('#channel-checkboxes');
  function updateChannelList() {
    const checkedGroups = Array.from(groupDiv.querySelectorAll('input:checked')).map(cb => cb.value);
    channelDiv.innerHTML = channels.filter(c => checkedGroups.includes(c.group)).map((c,i) => `<label style='display:block;margin-bottom:4px;'><input type='checkbox' class='channelbox' value='${i}' checked> ${c.name} <span style='color:#888;font-size:0.9em;'>[${c.group}]</span></label>`).join('');
  }
  groupDiv.addEventListener('change', updateChannelList);
  updateChannelList();
  modal.querySelector('#channel-select-form').onsubmit = function(e) {
    e.preventDefault();
    const checked = Array.from(channelDiv.querySelectorAll('input:checked')).map(cb => parseInt(cb.value));
    const selected = checked.map(i => channels[i]);
    // POST selection to backend
    fetch('channel_selection.json', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(selected)
    }).then(() => {
      modal.remove();
      location.reload();
    });
  };
}