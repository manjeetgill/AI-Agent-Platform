/** Backend-only client. Never expose the service key or external-user mapping to a browser. */
export class WhatsAppNotifications {
  private readonly origin: string;

  constructor(baseUrl: string, private readonly serviceKey: string) {
    const url = new URL(baseUrl);
    if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash || url.pathname !== "/") {
      throw new Error("Use the platform HTTPS origin");
    }
    this.origin = url.origin;
  }

  private async request(ownerId: string, method: string, path: string, body?: unknown) {
    const response = await fetch(`${this.origin}/v1/whatsapp/alerts/${path}`, {
      method,
      redirect: "error",
      headers: {
        Authorization: `Bearer ${this.serviceKey}`,
        "X-External-User-ID": ownerId,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(20_000),
    });
    const result = response.status === 204 ? undefined : await response.json();
    if (!response.ok) throw new Error(`Agent platform request failed (${response.status})`);
    return result;
  }

  bindOwnedSender(ownerId: string, channelId: string, dailyLimit = 20) {
    return this.request(ownerId, "PUT", "route", { channel_id: channelId, daily_limit: dailyLimit });
  }

  route(ownerId: string) {
    return this.request(ownerId, "GET", "route");
  }

  beginRecipientVerification(ownerId: string, recipient: string) {
    return this.request(ownerId, "POST", "verifications", { recipient });
  }

  verification(ownerId: string, verificationId: string) {
    return this.request(ownerId, "GET", `verifications/${verificationId}`);
  }

  subscribe(ownerId: string, recipient: string) {
    return this.request(ownerId, "PUT", "subscription", { recipient, consented: true });
  }

  notify(ownerId: string, payload: unknown) {
    // Use a durable business event ID as request_key. Reconcile before any retry.
    return this.request(ownerId, "POST", "notifications", payload);
  }

  delivery(ownerId: string, deliveryId: string) {
    return this.request(ownerId, "GET", `deliveries/${deliveryId}`);
  }
}
