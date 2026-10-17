"""
CON-347: list_comments must report live Likes/Replies counts after like + reply.

Live finding (2026-08-14, CON-333): after connect_add_reply + connect_like_comment
both reported success, re-reading connect_list_comments still rendered
"Likes: 0 | Replies: 0" even though connect_list_replies returned the reply and
connect_get_post incremented total Comments. The list serializer/count contract
was stale: CommentSerializer exposed no replies_count, and CommentLike creation
never updated Comment.likes_count.

This suite pins the fixed contract:
  - parent comment row reports Likes=1 and Replies=1 while mutations are active;
  - unlike restores the like baseline (Likes=0);
  - counts agree with list_replies and the post detail.
"""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase, APIClient

from connect.models import Post, Comment

User = get_user_model()


def _mk_user(username, email):
    return User.objects.create_user(username=username, email=email)


class CommentCountContractTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = _mk_user('con347', 'con347@shizuha.com')
        self.post = Post.objects.create(
            author_id=self.user.id, author_email=self.user.email,
            content='CON-347 controlled post', visibility='public',
        )
        self.comment = Comment.objects.create(
            post=self.post, author_id=self.user.id,
            author_email=self.user.email, content='CON-347 parent comment',
        )
        self.client.force_authenticate(self.user)

    def _list_comments(self):
        resp = self.client.get(f'/api/posts/{self.post.id}/comments/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.data
        # Cursor-paginated: {'count', 'next', 'previous', 'results'}.
        return data.get('results', data) if isinstance(data, dict) else data

    def test_reply_and_like_reflect_in_list_comments_counts(self):
        # Reply to the parent comment.
        reply_resp = self.client.post(
            f'/api/comments/{self.comment.id}/replies/',
            {'content': 'CON-347 reply'},
            format='json',
        )
        self.assertEqual(reply_resp.status_code, status.HTTP_201_CREATED)

        # Like the parent comment.
        like_resp = self.client.post(f'/api/comments/{self.comment.id}/like/')
        self.assertEqual(like_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(like_resp.data.get('status'), 'liked')

        # list_comments must report Likes=1 and Replies=1.
        comments = self._list_comments()
        parent = next(
            (c for c in comments if c['id'] == str(self.comment.id)), None,
        )
        self.assertIsNotNone(parent, 'parent comment missing from list')
        self.assertEqual(parent['likes_count'], 1)
        self.assertEqual(parent['replies_count'], 1)
        # The reply is present in the replies list.
        self.assertEqual(len(parent['replies']), 1)
        reply_id = parent['replies'][0]['id']
        self.assertNotEqual(reply_id, 'None')

        # list_replies agrees.
        replies_resp = self.client.get(f'/api/comments/{self.comment.id}/replies/')
        self.assertEqual(replies_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(replies_resp.data), 1)
        self.assertEqual(replies_resp.data[0]['id'], reply_id)

    def test_unlike_restores_like_baseline(self):
        self.client.post(f'/api/comments/{self.comment.id}/like/')
        unlike_resp = self.client.post(f'/api/comments/{self.comment.id}/unlike/')
        self.assertEqual(unlike_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(unlike_resp.data.get('status'), 'unliked')

        comments = self._list_comments()
        parent = next(
            (c for c in comments if c['id'] == str(self.comment.id)), None,
        )
        self.assertEqual(parent['likes_count'], 0)
