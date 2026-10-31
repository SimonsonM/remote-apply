"""Explicit, scoped permission checks for consequential actions."""
import datetime as dt

ACTIONS = {'submit', 'message', 'publish', 'spend', 'delete'}

def check(action, job, prof):
    if action not in ACTIONS:
        return False, 'unknown action'
    grant = (prof.get('permissions') or {}).get(action)
    if not isinstance(grant, dict) or grant.get('allowed') is not True:
        return False, f'{action} permission not granted'
    try:
        expires = dt.datetime.fromisoformat(grant['expires'].replace('Z', '+00:00'))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=dt.timezone.utc)
        if expires <= dt.datetime.now(dt.timezone.utc):
            return False, f'{action} permission expired'
    except (KeyError, TypeError, ValueError):
        return False, f'{action} permission has no valid expiry'
    scope = grant.get('scope')
    if not isinstance(scope, dict):
        return False, f'{action} permission has no scope'
    if action == 'submit':
        companies = scope.get('companies')
        if companies is not None and job.get('company') not in companies:
            return False, 'company outside submit scope'
        cap = scope.get('daily_cap')
        if cap is not None and (not isinstance(cap, int) or cap <= 0 or job.get('submitted_today', 0) >= cap):
            return False, 'submit daily cap reached'
    if action == 'publish':
        if not isinstance(scope.get('platforms'), list) or not isinstance(scope.get('experiments'), list):
            return False, 'publish scope requires platforms and experiments'
        if job.get('platform') not in scope['platforms'] or job.get('experiment') not in scope['experiments']:
            return False, 'publication outside scope'
    return True, 'permitted'
