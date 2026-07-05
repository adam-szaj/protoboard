# Example depicting how registering Renderers may look like
```
def register_renderer(**kwargs):
    # implement me
    def _wrapper(cls, **kwargs):
        print(f"register renderer: {cls} kwargs: {kwargs}")
        return cls

    return _wrapper


@register_renderer(shape_name="prism")
class PrismRenderer:
    pass


@register_renderer(component_class="led_bar")
class LedBarRenderer(PrismRenderer):
    pass
```


## Other keys Example list:
- shape_name
- component_class
- component_name
- package_kind
- package_name
- ...
