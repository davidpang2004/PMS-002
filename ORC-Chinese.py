import pytesseract
from PIL import Image11


text = pytesseract.image_to_string(
    Image.open("image.png"),
    lang="eng+chi_sim"
)
print(text)