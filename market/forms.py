from decimal import Decimal

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from .models import Customer, DeliveryAddress, Product, Vendor


class VendorSearchForm(forms.Form):
    name = forms.CharField(
        label='Vendor name',
        max_length=100,
        required=False,
    )


class ProductSearchForm(forms.Form):
    name = forms.CharField(
        label='Search products',
        max_length=100,
        required=False,
    )


class VendorRegistrationForm(forms.Form):
    username = forms.CharField(max_length=150)
    email = forms.EmailField()
    vendor_name = forms.CharField(max_length=100, label='Vendor name')
    shop_name = forms.CharField(max_length=100, label='Shop name')
    shop_address = forms.CharField(
        max_length=250,
        label='Shop address',
        widget=forms.Textarea(attrs={'rows': 3}),
    )
    business_registration = forms.CharField(
        max_length=100,
        label='Business registration or license number',
        required=False,
    )
    description = forms.CharField(
        label='Shop description',
        required=False,
        widget=forms.Textarea(attrs={'rows': 4}),
    )
    phone = forms.CharField(max_length=20)
    password = forms.CharField(widget=forms.PasswordInput)
    password_confirm = forms.CharField(
        label='Confirm password',
        widget=forms.PasswordInput,
    )

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError('That username is already in use.')
        return username

    def clean_vendor_name(self):
        vendor_name = self.cleaned_data['vendor_name'].strip()
        if Vendor.objects.filter(name__iexact=vendor_name).exists():
            raise ValidationError('A vendor with this name already exists.')
        return vendor_name

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get('password')
        password_confirm = cleaned_data.get('password_confirm')
        if password and password_confirm and password != password_confirm:
            self.add_error('password_confirm', 'Passwords do not match.')

        if password and cleaned_data.get('username'):
            candidate_user = User(
                username=cleaned_data['username'],
                email=cleaned_data.get('email', ''),
            )
            try:
                validate_password(password, user=candidate_user)
            except ValidationError as error:
                self.add_error('password', error)
        return cleaned_data


class VendorAuthenticationForm(AuthenticationForm):
    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not Vendor.objects.filter(user=user).exists():
            raise ValidationError(
                'Use the customer login page for a customer account.',
                code='not_vendor',
            )


class ProductForm(forms.ModelForm):
    price = forms.DecimalField(
        min_value=Decimal('0.01'),
        max_digits=10,
        decimal_places=2,
        label='Price',
    )
    stock = forms.IntegerField(min_value=0, label='Stock')

    class Meta:
        model = Product
        fields = (
            'name',
            'category',
            'price',
            'units',
            'stock',
            'description',
            'image',
        )


class StockUpdateForm(forms.Form):
    stock = forms.IntegerField(min_value=0, label='Stock')


class CustomerUserProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ('first_name', 'last_name', 'email')


class CustomerProfileForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ('phone',)


class DeliveryAddressForm(forms.ModelForm):
    class Meta:
        model = DeliveryAddress
        fields = (
            'label',
            'recipient_name',
            'address_line_1',
            'address_line_2',
            'city',
            'state',
            'postal_code',
            'phone',
        )
