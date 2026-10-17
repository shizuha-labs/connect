"""
Custom middleware for handling reverse proxy configurations.
"""


class ScriptNameMiddleware:
    """
    Middleware to handle the X-Script-Name header from the reverse proxy (Nginx).
    This dynamically sets the URL prefix for each request, which is more reliable
    than the global FORCE_SCRIPT_NAME setting, especially for complex redirect
    scenarios like admin login.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Nginx sends X-Script-Name so public URLs stay under /connect even
        # though Daphne sees PATH_INFO without that prefix. Django snapshots
        # request.path at ASGI construction; mutating META alone does not
        # update it, so pagination `next` would omit /connect.
        script_name = request.META.get('HTTP_X_SCRIPT_NAME')
        if script_name:
            if not script_name.startswith('/'):
                script_name = '/' + script_name
            script_name = script_name.rstrip('/')
            request.META['SCRIPT_NAME'] = script_name
            path_info = request.path_info or request.META.get('PATH_INFO') or '/'
            if script_name and path_info.startswith(script_name):
                path_info = path_info[len(script_name):] or '/'
            if not path_info.startswith('/'):
                path_info = '/' + path_info
            request.META['PATH_INFO'] = path_info
            request.path_info = path_info
            request.path = f'{script_name}{path_info}'

        response = self.get_response(request)
        return response
