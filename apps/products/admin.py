from django.contrib import admin
from .models import Category, Brand, Unit, Product, ProductImage, ProductVariant, PriceList, ProductPrice


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'organization', 'slug', 'parent', 'is_active', 'sort_order']
    list_filter = ['is_active', 'organization']
    search_fields = ['name', 'slug']
    prepopulated_fields = {'slug': ('name',)}
    raw_id_fields = ['organization', 'parent']


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ['name', 'organization', 'is_active']
    list_filter = ['is_active', 'organization']
    search_fields = ['name', 'slug']
    prepopulated_fields = {'slug': ('name',)}
    raw_id_fields = ['organization']


@admin.register(Unit)
class UnitAdmin(admin.ModelAdmin):
    list_display = ['name', 'symbol', 'organization', 'base_unit', 'conversion_factor']
    search_fields = ['name', 'symbol']
    raw_id_fields = ['organization', 'base_unit']


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 1


class ProductVariantInline(admin.TabularInline):
    model = ProductVariant
    extra = 0


class ProductPriceInline(admin.TabularInline):
    model = ProductPrice
    extra = 0


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ['name', 'sku', 'organization', 'category', 'selling_price', 'is_active']
    list_filter = ['is_active', 'category', 'brand', 'organization']
    search_fields = ['name', 'sku', 'barcode']
    prepopulated_fields = {'slug': ('name',)}
    raw_id_fields = ['organization', 'category', 'brand', 'unit', 'created_by']
    inlines = [ProductImageInline, ProductVariantInline, ProductPriceInline]
    
    fieldsets = (
        (None, {
            'fields': ('organization', 'name', 'slug', 'sku', 'barcode')
        }),
        ('Description', {
            'fields': ('short_description', 'image')
        }),
        ('Classification', {
            'fields': ('category', 'brand', 'unit')
        }),
        ('Pricing', {
            'fields': ('cost_price', 'selling_price', 'wholesale_price', 'tax_rate', 'is_taxable')
        }),
        ('Inventory', {
            'fields': ('track_inventory', 'allow_negative_stock', 'min_stock_level', 
                      'max_stock_level', 'reorder_point', 'reorder_quantity')
        }),
        ('Tracking', {
            'fields': ('expiry_tracking', 'batch_tracking', 'serial_tracking')
        }),
        ('Status', {
            'fields': ('is_active', 'is_featured', 'is_sellable', 'is_purchasable')
        }),
        ('Additional', {
            'fields': ('weight', 'dimensions', 'attributes', 'created_by'),
            'classes': ('collapse',)
        }),
    )


@admin.register(PriceList)
class PriceListAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'organization', 'is_default', 'is_active']
    list_filter = ['is_active', 'is_default', 'organization']
    search_fields = ['name', 'code']
    raw_id_fields = ['organization']
