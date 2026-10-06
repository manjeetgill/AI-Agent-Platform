// Mount in your signed-in app. These routes belong to YOUR backend, which stores
// the business-scoped service key. Never put the platform service key in JavaScript.
export async function connectWhatsApp(container, status, backend = '/api/whatsapp') {
  async function request(path, body) {
    const response = await fetch(backend + path, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
      signal: AbortSignal.timeout(20000),
    });
    if (!response.ok) throw new Error('Connection could not complete. Check its saved status.');
    return response.json();
  }
  status.textContent = 'Preparing WhatsApp connection…';
  const session = await request('/onboarding', {});
  if (!window.ChakraWhatsappConnect) {
    await new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = 'https://embed.chakrahq.com/whatsapp-partner-connect/v1_0_1/sdk.js';
      script.onload = resolve; script.onerror = reject; document.head.appendChild(script);
    });
  }
  const widget = window.ChakraWhatsappConnect.init({
    connectToken: session.connect_token,
    container,
    onReady: () => { status.textContent = 'Connect your business number with Facebook.'; },
    onError: () => { status.textContent = 'Connection interrupted. Check your saved connection before restarting.'; },
    onSuccess: async data => {
      try {
        const result = await request(`/onboarding/${encodeURIComponent(session.id)}/complete`, {plugin_id: data.pluginId});
        status.textContent = result.message || 'Authorization received. Checking your business connection.';
      } catch {
        status.textContent = 'Authorization received, but confirmation is pending. Check saved status.';
      }
    },
  });
  return () => widget.destroy();
}
