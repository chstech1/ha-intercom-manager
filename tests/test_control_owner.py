"""An otherwise identical HA principal on another control connection is not owner."""
import check_real_media  # Adds the candidate component to sys.path.
from session import Principal, Refused, Session

session = Session()
principal = Principal('user', 'token', 'connection-a', 'nabu_remote', 'https://example')
other = Principal('user', 'token', 'connection-b', 'nabu_remote', 'https://example')
owner = session.start(principal, b'b' * 16)
assert session.require_owner(principal, owner.call_id, owner.generation) is owner
try:
    session.require_owner(other, owner.call_id, owner.generation)
except Refused as err:
    assert str(err) == 'NOT_OWNER'
else:
    raise AssertionError('another control connection accepted as call owner')
print('PASS: same user and token on another control connection cannot own call')
