from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('pyqapp', '0011_alter_iqdownload_unique_together_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='paper',
            name='hashtags',
            field=models.CharField(blank=True, max_length=1000, help_text='e.g. java, oopj'),
        ),
    ]
