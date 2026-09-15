/** Protocol-compatible r3 queue, independent of React and ketcher-core. */
export class IndigoTransport {
  constructor(worker) {
    this.worker = worker;
    this.queue = [];
    this.active = null;
    this.disposed = false;
    worker.onmessage = event => {
      if (this.disposed) return;
      const message = event.data;
      const task = this.active;
      if (!task || !message || message.type !== task.message.type ||
          typeof message.hasError !== 'boolean' ||
          ([1, 10].includes(message.type) && message.inputData !== task.message.data.struct)) {
        this.destroy(new Error('Invalid Indigo Worker response'));
        return;
      }
      clearTimeout(task.timer);
      this.active = null;
      try { task.action({ data: message }); }
      catch (error) { task.reject(error); this.destroy(error); return; }
      this.drainQueue();
    };
    worker.onerror = event => {
      event.preventDefault();
      this.destroy(new Error(event.message || 'Indigo Worker failed'));
    };
    worker.onmessageerror = () => this.destroy(new Error('Unreadable Indigo Worker response'));
  }

  dispatch(message, action, reject) {
    if (this.disposed) { reject(this.failure); return; }
    this.queue.push({ message, action, reject });
    this.drainQueue();
  }

  info() {
    if (this.disposed) return Promise.reject(this.failure);
    if (!this.infoPromise) {
      this.infoPromise = new Promise((resolve, reject) => {
        this.dispatch({ type: 0 }, ({ data }) => {
          if (data.hasError) reject(new Error(String(data.error)));
          else resolve({ indigoVersion: data.payload, imagoVersions: [], isAvailable: true });
        }, reject);
      });
      // Preheating has no consumer until a visible session takes ownership.
      this.infoPromise.catch(() => {});
    }
    return this.infoPromise;
  }

  drainQueue() {
    if (this.disposed || this.active || !this.queue.length) return;
    const task = this.active = this.queue.shift();
    task.timer = setTimeout(() => this.destroy(new Error('Indigo Worker request timed out')),
      task.message.type === 0 ? 15000 : 8000);
    try { this.worker.postMessage(task.message); }
    catch (error) { this.destroy(error); }
  }

  destroy(error = new DOMException('Structure service disposed', 'AbortError')) {
    if (this.disposed) return;
    this.disposed = true;
    this.failure = error;
    const tasks = this.queue.splice(0);
    if (this.active) tasks.unshift(this.active);
    this.active = null;
    this.worker.onmessage = this.worker.onerror = this.worker.onmessageerror = null;
    this.worker.terminate();
    for (const task of tasks) { clearTimeout(task.timer); task.reject(error); }
  }
}
