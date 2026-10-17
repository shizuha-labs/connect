from rest_framework.permissions import BasePermission


class ServiceAccessPermission(BasePermission):
    SERVICE_SLUG = 'connect'
    message = 'You do not have access to this service.'

    def has_permission(self, request, view):
        user = request.user
        if not hasattr(user, 'enabled_services'):
            return True  # Not a federated user (e.g., Django admin)
        if user.enabled_services is None:
            return True  # Unrestricted (staff/superuser)
        return self.SERVICE_SLUG in user.enabled_services
