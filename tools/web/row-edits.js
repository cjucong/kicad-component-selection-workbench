/* Serialize writes and retain edits made while an earlier request is in flight. */
function createRowEditQueue({save, onError = () => {}, onIdle = () => {}, delay = 650}) {
  const edits = new Map(), paused = new Set();
  let timer = null, running = null;
  function stopTimer() { clearTimeout(timer); timer = null; }
  async function drain() {
    const failed = new Map();
    for (;;) {
      const next = [...edits].find(([key, entry]) => !paused.has(key) && failed.get(key)?.entry !== entry);
      if (!next) {
        const remaining = [...failed].find(([key, failure]) => edits.get(key) === failure.entry);
        if (remaining) throw remaining[1].error;
        await onIdle();
        if (![...edits.keys()].some(key => !paused.has(key))) return;
        continue;
      }
      const [key, entry] = next;
      try {
        await save(entry);
        if (edits.get(key) === entry) edits.delete(key);
        failed.delete(key);
      } catch (error) { failed.set(key, {entry, error}); }
    }
  }
  function flush() {
    stopTimer();
    if (!running) running = Promise.resolve().then(drain).finally(() => { running = null; });
    return running;
  }
  return {
    schedule(key, entry) {
      edits.set(key, entry);
      paused.delete(key);
      stopTimer();
      timer = setTimeout(() => { flush().catch(onError); }, delay);
    },
    pause(key) { paused.add(key); },
    has(key) { return edits.has(key); },
    flush,
    get pending() { return edits.size > 0; }
  };
}
if (typeof module !== 'undefined') module.exports = {createRowEditQueue};
