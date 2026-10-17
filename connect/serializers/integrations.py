from rest_framework import serializers

from connect.linkedin_credentials import public_linkedin_status
from connect.models.integrations import ExternalAccount


class ExternalAccountSerializer(serializers.ModelSerializer):
    has_credentials = serializers.SerializerMethodField()

    class Meta:
        model = ExternalAccount
        fields = (
            'id', 'provider', 'status', 'email', 'profile_url',
            'display_name', 'secret_ref', 'capabilities', 'connected_at',
            'updated_at', 'has_credentials',
        )
        read_only_fields = fields

    def get_has_credentials(self, obj):
        if obj.provider != 'linkedin':
            return False
        return public_linkedin_status().get('has_credentials', False)
