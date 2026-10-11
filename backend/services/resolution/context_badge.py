"""Portable bitmap labels for context-video exports."""
def render_cue(path, cue, width):
    from PIL import Image, ImageDraw, ImageFont
    scale = width / 640
    font = ImageFont.load_default(size=max(12, int(16*scale)))
    small = ImageFont.load_default(size=max(10, int(11*scale)))
    w, h = int(236*scale), int(58*scale)
    badge = Image.new('RGBA', (w,h), (0,0,0,0))
    draw = ImageDraw.Draw(badge)
    draw.rounded_rectangle((0,0,w-1,h-1), radius=int(9*scale), fill=(12,20,30,225))
    draw.rounded_rectangle((int(9*scale),int(12*scale),int(13*scale),h-int(12*scale)), radius=2, fill=cue['color'])
    draw.text((int(22*scale),int(11*scale)),cue['label'],font=font,fill=cue['color'])
    draw.text((int(22*scale),int(34*scale)),'Original source footage',font=small,fill='white')
    badge.save(path)
