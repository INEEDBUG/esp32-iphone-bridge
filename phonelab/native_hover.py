"""Recognize one explicitly selected blue native control, not a pointer pose."""
from .synthetic_vision import _srgb_image


def verify_unique_blue_hover(png,labels,expected,icon_bounds,*,baseline_png):
    image=_srgb_image(png)
    baseline=_srgb_image(baseline_png)
    if (image is None or baseline is None or image.size!=baseline.size
            or not isinstance(labels,dict) or set(labels)!= {'home','device','analysis','siri'}
            or expected!='home'):
        raise ValueError('known native menu geometry required')
    def valid_bounds(bounds):
        if (not isinstance(bounds,(list,tuple)) or len(bounds)!=4
                or any(type(x) is not int for x in bounds)
                or not 0<=bounds[0]<bounds[2]<=image.width
                or not 0<=bounds[1]<bounds[3]<=image.height):
            raise ValueError('invalid native target geometry')
    regions=[*labels.values(),icon_bounds]
    for bounds in regions:
        valid_bounds(bounds)
    for i,left in enumerate(regions):
        for right in regions[i+1:]:
            if left[0]<right[2] and right[0]<left[2] and left[1]<right[3] and right[1]<left[3]:
                raise ValueError('native target geometries must be disjoint')

    def counts(bounds):
        current=image.crop(bounds).tobytes();before=baseline.crop(bounds).tobytes()
        blue_count=baseline_blue_count=baseline_light_count=overlap_count=0
        for i in range(0,len(current),3):
            r,g,b=current[i:i+3];br,bg,bb=before[i:i+3]
            blue=b>=120 and g>=60 and b-r>=60 and g-r>=20
            baseline_blue=bb>=120 and bg>=60 and bb-br>=60 and bg-br>=20
            light=min(br,bg,bb)>=160 and max(br,bg,bb)-min(br,bg,bb)<=24
            blue_count+=blue
            baseline_blue_count+=baseline_blue
            baseline_light_count+=light
            overlap_count+=blue and light
        return blue_count,baseline_light_count,overlap_count,baseline_blue_count

    home_blue,home_light,home_overlap,baseline_home_blue=counts(labels[expected])
    icon_blue,icon_light,icon_overlap,baseline_icon_blue=counts(icon_bounds)
    other_blue={key:counts(bounds)[0] for key,bounds in labels.items() if key!=expected}
    if (baseline_home_blue or baseline_icon_blue or home_light<80 or icon_light<80
            or home_blue<80 or icon_blue<80 or home_overlap<80 or icon_overlap<80
            or any(other_blue.values())):
        raise ValueError('unique native Home highlight unavailable')
    return {'target':expected,'evidence':'native_blue_highlight','pointer_position':None}
