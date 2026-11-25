from apps.people.access import WorkspaceAccess


def workspace(request):
    """Expose navigation hints; the destination views still authorize each request."""
    return {"workspace": WorkspaceAccess(request.user)}
