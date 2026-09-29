"""Default-deny API policy. Domain resources additionally enforce owner + RLS."""
from __future__ import annotations

import re

MODULES = ('database','prediction','generation','polytao','reverse','knowledge','online_knowledge',
           'browsing','assistant','md','dft','polymerization','batch','retrosynthesis','openscience')


def capabilities(member: bool) -> dict[str, bool]:
    return {'examples.view': True, 'templates.download': True, 'canvas.local': True,
            **{f'{module}.use': member for module in MODULES},
            'lab.use': False, 'lab_workflow.use': False, 'operations.use': False}


def route_policy(path: str, method: str) -> str:
    if path.startswith('/internal/'):
        return 'internal'
    if path in {'/health','/api/v1/auth/session','/api/v1/auth/login'}:
        return 'public'
    if method in {'GET','HEAD'} and re.fullmatch(r'/api/v1/monomer-polymerization/batch/templates/[ab]\.(csv|xlsx)', path):
        return 'public'
    if path.startswith(('/api/v1/lab', '/api/v1/dev-gpu')):
        return 'operations'
    if path in {'/docs','/openapi.json','/redoc'}:
        return 'operations'
    return 'member'
