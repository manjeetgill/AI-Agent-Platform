# App integration readiness

An authenticated consuming backend can call `GET /v1/integrations/readiness` with
its service key and stable `X-External-User-ID`. The response reports the key's
feature grants and only that app/user's saved WhatsApp channels, notification route,
recipient consent, WhatsApp calling agents, web voice agents, telephone gateways,
validated memories and trial policy.

The endpoint accepts any valid app service key because it returns only the identity
and resources already bound to that key. It performs no provider request and no
mutation. Use it to render setup progress and decide which dedicated readiness API
to call next. It must not be shown as proof that Meta calling is eligible, a carrier
route works, the model has quota, audio traverses the network, or a message reached
the recipient. Those claims require the provider-specific checks and real tests.

Unavailable agent services should affect only the consuming app's agent screen.
Set a bounded request timeout, show the component as unavailable, and keep the rest
of the consuming application operational.
