"""
Profile serializers for Shizuha Connect.
"""

from rest_framework import serializers
from connect.models import (
    SocialProfile, Skill, UserSkill, Endorsement, Experience, Education
)


class SkillSerializer(serializers.ModelSerializer):
    class Meta:
        model = Skill
        fields = ['id', 'name', 'slug', 'category']
        read_only_fields = ['id', 'slug']


class EndorsementSerializer(serializers.ModelSerializer):
    class Meta:
        model = Endorsement
        fields = ['id', 'endorser_id', 'endorser_email', 'created_at']
        read_only_fields = ['id', 'created_at']


class UserSkillSerializer(serializers.ModelSerializer):
    skill = SkillSerializer(read_only=True)
    endorsements = EndorsementSerializer(many=True, read_only=True)

    class Meta:
        model = UserSkill
        fields = ['id', 'skill', 'endorsement_count', 'display_order', 'endorsements']
        read_only_fields = ['id', 'endorsement_count']


class ExperienceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Experience
        fields = [
            'id', 'title', 'company_name', 'employer_id', 'location',
            'employment_type', 'start_date', 'end_date', 'is_current', 'description'
        ]
        read_only_fields = ['id']


class EducationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Education
        fields = [
            'id', 'school_name', 'degree', 'field_of_study',
            'start_date', 'end_date', 'description', 'activities'
        ]
        read_only_fields = ['id']


class SocialProfileSerializer(serializers.ModelSerializer):
    skills = UserSkillSerializer(many=True, read_only=True)
    experiences = ExperienceSerializer(many=True, read_only=True)
    education = EducationSerializer(many=True, read_only=True)

    class Meta:
        model = SocialProfile
        fields = [
            'id', 'user_id', 'user_email', 'display_name', 'username',
            'bio', 'headline', 'avatar_url', 'banner_url', 'location',
            'website', 'current_position', 'current_company', 'industry',
            'profile_visibility', 'followers_count', 'following_count',
            'posts_count', 'connections_count', 'is_verified', 'verified_type',
            'created_at', 'updated_at', 'last_active_at',
            'skills', 'experiences', 'education'
        ]
        read_only_fields = [
            'id', 'user_id', 'user_email', 'followers_count', 'following_count',
            'posts_count', 'connections_count', 'is_verified', 'verified_type',
            'created_at', 'updated_at'
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # CON-142 P1.2: user_email is PII. The profile read plane returned it to
        # every viewer, so any authenticated user (any org) could harvest every
        # user's email. Only expose it to the owner or a viewer who shares an
        # active org with the profile owner; fail closed with no request context.
        request = self.context.get('request')
        viewer_id = getattr(getattr(request, 'user', None), 'id', None)
        if viewer_id is not None and viewer_id == instance.user_id:
            return data
        from connect.services.privacy import users_share_org
        if viewer_id is None or not users_share_org(viewer_id, instance.user_id):
            data.pop('user_email', None)
        return data


class SocialProfileCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = SocialProfile
        fields = [
            'display_name', 'username', 'bio', 'headline',
            'avatar_url', 'banner_url', 'location', 'website',
            'current_position', 'current_company', 'industry',
            'profile_visibility'
        ]


class SocialProfileUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = SocialProfile
        fields = [
            'display_name', 'bio', 'headline', 'avatar_url', 'banner_url',
            'location', 'website', 'current_position', 'current_company',
            'industry', 'profile_visibility'
        ]


class SocialProfileMinimalSerializer(serializers.ModelSerializer):
    """Minimal profile info for embedding in other responses."""

    class Meta:
        model = SocialProfile
        fields = [
            'id', 'user_id', 'display_name', 'username',
            'avatar_url', 'headline', 'is_verified'
        ]
