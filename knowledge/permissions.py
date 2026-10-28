from rest_framework import permissions


class IsOwnerOrReadOnly(permissions.BasePermission):
    """Allow read for any authenticated user, write only for the owner or staff."""

    owner_field = "created_by"

    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True
        if request.user.is_staff:
            return True
        owner = getattr(obj, self.owner_field, None)
        return owner is not None and owner == request.user


class IsStaffOrReadOnly(permissions.BasePermission):
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return request.user and request.user.is_authenticated
        return request.user and request.user.is_staff
