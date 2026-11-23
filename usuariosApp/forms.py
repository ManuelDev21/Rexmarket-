import re
from itertools import cycle
from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.contrib.auth.forms import UserCreationForm, PasswordResetForm

from usuariosApp.models import Producto, Comentario, Sede


class ProductoForm(forms.ModelForm):
    class Meta:
        model = Producto
        fields = ['nombre', 'descripcion', 'precio', 'imagen', 'categoria', 'sede', 'acepta_envio']

    imagen = forms.ImageField(
        required=True,
        widget=forms.ClearableFileInput(attrs={'class': 'form-control-file'}),
        error_messages={
            'required': 'La imagen del producto es obligatoria.',
            'invalid': 'Sube una imagen válida.',
        }
    )

    categoria = forms.ChoiceField(
        choices=Producto.CATEGORIAS,
        required=True,
        widget=forms.Select(attrs={'class': 'form-control'}),
        error_messages={
            'required': 'Selecciona una categoría válida.',
        }
    )

    sede = forms.ModelChoiceField(
        queryset=Sede.objects.filter(activa=True),
        required=True,
        empty_label="Selecciona la sede de entrega...",
        widget=forms.Select(attrs={'class': 'form-control'}),
        error_messages={
            'required': 'Debes seleccionar una sede de INACAP para el producto.',
        }
    )

    acepta_envio = forms.BooleanField(
        required=False,
        label="Acepto realizar envíos por encomienda a estudiantes de otras regiones",
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'})
    )

    precio = forms.CharField(
        max_length=20,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Ej: 15.000'}),
        validators=[
            RegexValidator(
                r'^\$?\s*\d{1,3}(?:[.,]\d{3})*(?:,\d+)?$|^\d+$',
                'El precio debe ser un número válido (ej. 15000 o 15.000).'
            )
        ]
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            if field_name == 'imagen':
                field.widget.attrs.setdefault('class', 'form-control-file')
            elif field_name == 'acepta_envio':
                field.widget.attrs.setdefault('class', 'form-check-input')
            else:
                field.widget.attrs.setdefault('class', 'form-control')

    def clean_nombre(self):
        nombre = self.cleaned_data.get('nombre', '').strip()
        if len(nombre) < 3:
            raise ValidationError("El nombre del producto debe tener al menos 3 caracteres.")
        return nombre[0].upper() + nombre[1:] if nombre else nombre

    def clean_descripcion(self):
        descripcion = self.cleaned_data.get('descripcion', '').strip()
        if len(descripcion) < 10:
            raise ValidationError("La descripción debe tener al menos 10 caracteres.")
        return descripcion

    def clean_precio(self):
        precio = self.cleaned_data.get('precio', '').strip().replace('$', '').strip()
        precio_normalizado = precio.replace('.', '').replace(',', '.')
        try:
            precio_float = float(precio_normalizado)
            if precio_float <= 0:
                raise ValidationError("El precio debe ser un valor mayor que 0.")
        except ValueError:
            raise ValidationError("El precio debe ser un número válido.")
        return precio


def validar_rut(rut: str) -> bool:
    if not rut:
        return False
    rut = rut.upper().strip().replace(".", "").replace("-", "")
    if len(rut) < 2:
        return False

    cuerpo, dv = rut[:-1], rut[-1:]
    if not cuerpo.isdigit():
        return False

    revertido = map(int, reversed(cuerpo))
    factors = cycle(range(2, 8))
    s = sum(d * f for d, f in zip(revertido, factors))
    res = (-s) % 11

    if str(res) == dv:
        return True
    if dv == "K" and res == 10:
        return True
    return False


class CustomUserCreationForm(UserCreationForm):
    region = forms.ChoiceField(
        choices=[('', 'Selecciona tu región...')] + Sede.REGIONES_CHILE,
        required=True,
        label="Región",
        error_messages={'required': 'Selecciona tu región.'}
    )
    sede = forms.ModelChoiceField(
        queryset=Sede.objects.filter(activa=True),
        required=True,
        empty_label="Primero selecciona una región...",
        label="Sede INACAP",
        error_messages={'required': 'Selecciona tu sede de INACAP.'}
    )

    class Meta:
        model = User
        fields = ('email', 'first_name', 'last_name', 'username', 'region', 'sede', 'password1', 'password2')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            if field_name in ('region', 'sede'):
                field.widget.attrs['class'] = 'form-select'
            else:
                field.widget.attrs['class'] = 'form-control'

    def clean_username(self):
        username = self.cleaned_data.get('username')
        if not username:
            return username

        rut_limpio = username.upper().replace(".", "").replace("-", "").strip()
        if not validar_rut(rut_limpio):
            raise ValidationError("El RUT ingresado no es válido.")

        cuerpo = rut_limpio[:-1]
        dv = rut_limpio[-1]
        rut_formateado = f"{cuerpo}-{dv}"

        if User.objects.filter(username=rut_formateado).exists():
            raise ValidationError("Ya existe una cuenta registrada con este RUT.")

        return rut_formateado

    def clean_first_name(self):
        first_name = self.cleaned_data.get('first_name', '').strip()
        if not first_name:
            raise ValidationError("El nombre es obligatorio.")
        return first_name.title()

    def clean_last_name(self):
        last_name = self.cleaned_data.get('last_name', '').strip()
        if not last_name:
            raise ValidationError("El apellido es obligatorio.")
        return last_name.title()

    def clean_email(self):
        email = self.cleaned_data.get('email', '').strip().lower()
        if not email:
            raise ValidationError("El correo electrónico es obligatorio.")

        if not email.endswith('@inacapmail.cl'):
            raise ValidationError("Para registrarte en RexMarket debes usar tu correo institucional (@inacapmail.cl).")

        if User.objects.filter(email=email).exists():
            raise ValidationError("Este correo institucional ya está registrado en el sistema.")

        return email

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password1")
        password_confirm = cleaned_data.get("password2")
        region = cleaned_data.get("region")
        sede = cleaned_data.get("sede")

        if password and password_confirm and password != password_confirm:
            self.add_error('password2', "Las contraseñas no coinciden. Por favor, inténtalo de nuevo.")

        if region and sede and sede.region != region:
            self.add_error('sede', "La sede seleccionada no pertenece a la región indicada.")

        return cleaned_data


class ComentarioForm(forms.ModelForm):
    class Meta:
        model = Comentario
        fields = ['comentario', 'valoracion']
        widgets = {
            'comentario': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'Comparte tu opinión sobre este producto...'}),
            'valoracion': forms.NumberInput(attrs={'class': 'form-control', 'min': 1, 'max': 5}),
        }


class InstitutionalPasswordResetForm(PasswordResetForm):
    def clean_email(self):
        email = self.cleaned_data.get('email', '').strip().lower()
        if not email.endswith('@inacapmail.cl'):
            raise ValidationError(
                "Debes ingresar un correo institucional válido terminado en @inacapmail.cl."
            )
        return email