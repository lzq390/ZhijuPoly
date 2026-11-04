"""Default-deny API policy. Domain resources additionally enforce owner + RLS."""
from __future__ import annotations

import re

MODULES = ('database','prediction','generation','polytao','reverse','knowledge','online_knowledge',
           'browsing','assistant','md','dft','polymerization','batch','retrosynthesis','openscience')
SIMULATION_CHECKOUT_PATHS = frozenset({
    '/api/v1/billing/simulation/checkout', '/api/v1/billing/simulation/confirm'})


def capabilities(member: bool, *, acceptance=None, owner_id: str | None = None) -> dict[str, bool]:
    result = {'examples.view': True, 'templates.download': True, 'canvas.local': True,
            **{f'{module}.use': member for module in MODULES},
            'lab.use': False, 'lab.data.read': False, 'lab.data.write': False,
            'dev_gpu.status.read': False, 'home.embed': False,
            'lab_workflow.use': False, 'operations.use': False}
    if member and acceptance is not None and owner_id is not None:
        for name in ('lab.use', 'lab.data.read', 'lab.data.write', 'dev_gpu.status.read', 'home.embed'):
            result[name] = acceptance.allows(owner_id, name)
    return result


def route_policy(path: str, method: str) -> str:
    if method == 'POST' and path in SIMULATION_CHECKOUT_PATHS:
        return 'public'
    if method == 'POST' and path in {'/api/v1/billing/webhooks/wechat', '/api/v1/billing/webhooks/alipay'}:
        return 'payment_webhook'
    if path.startswith('/internal/'):
        return 'internal'
    if path in {'/health','/api/v1/auth/session','/api/v1/auth/login','/api/v1/auth/register','/api/v1/billing/plans'}:
        return 'public'
    if method == 'POST' and re.fullmatch(r'/api/v1/billing/orders/[0-9a-fA-F-]+/simulate-payment', path):
        return 'account'
    if path == '/api/v1/billing/subscription' or path == '/api/v1/billing/orders' or re.fullmatch(r'/api/v1/billing/orders/[0-9a-fA-F-]+', path):
        return 'account'
    if method in {'GET','HEAD'} and re.fullmatch(r'/api/v1/monomer-polymerization/batch/templates/[ab]\.(csv|xlsx)', path):
        return 'public'
    if method in {'GET', 'HEAD'} and path in {
        '/api/v1/lab-data/test-projects', '/api/v1/lab-data/sample-measurements',
        '/api/v1/lab-data/sample-measurements/count',
        '/api/v1/lab-data/sample-measurements/stats/by-project',
        '/api/v1/lab-data/sample-measurements/stats/summary',
    }:
        return 'capability:lab.data.read'
    if method == 'POST' and path == '/api/v1/lab-data/sample-measurements':
        return 'capability:lab.data.write'
    if method in {'GET', 'HEAD'} and path == '/api/v1/dev-gpu-session/status':
        return 'capability:dev_gpu.status.read'
    if path.startswith(('/api/v1/lab', '/api/v1/dev-gpu')):
        return 'operations'
    if path in {'/docs','/openapi.json','/redoc'}:
        return 'operations'
    return 'member'
