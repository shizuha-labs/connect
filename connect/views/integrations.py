"""External integrations (LinkedIn jobs, …) for the authenticated Connect user."""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from connect.linkedin_credentials import public_linkedin_status
from connect.models.integrations import ExternalAccount
from connect.serializers.integrations import ExternalAccountSerializer


class IntegrationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = ExternalAccount.objects.filter(user_id=request.user.id).order_by('provider')
        return Response({
            'results': ExternalAccountSerializer(rows, many=True).data,
        })


class LinkedInIntegrationView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        creds = public_linkedin_status()
        try:
            acc = ExternalAccount.objects.get(user_id=request.user.id, provider='linkedin')
        except ExternalAccount.DoesNotExist:
            return Response({
                'provider': 'linkedin',
                'status': 'disconnected',
                'email': creds.get('email') or '',
                'profile_url': creds.get('profile_url') or '',
                'capabilities': [],
                'has_credentials': creds.get('has_credentials', False),
            })
        data = ExternalAccountSerializer(acc).data
        if creds.get('email') and not data.get('email'):
            data['email'] = creds['email']
        if creds.get('profile_url') and not data.get('profile_url'):
            data['profile_url'] = creds['profile_url']
        data['has_credentials'] = creds.get('has_credentials', False)
        return Response(data)
