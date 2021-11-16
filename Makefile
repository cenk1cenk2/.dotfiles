deploy:
	stow --adopt -vvvt ~ $(TARGET)

plan:
	stow --adopt -nvvvt ~ $(TARGET)
